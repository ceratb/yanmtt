from transformers import AutoTokenizer
import time

import transformers

from transformers import MBartForConditionalGeneration, MBartConfig, get_linear_schedule_with_warmup
from transformers import AdamW


import os

import argparse

os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"   # see issue #152
#os.environ["CUDA_VISIBLE_DEVICES"]="0,1,2,3,4,5,6,7"

import torch
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel
import torch.multiprocessing as mp
import sys
import torch.distributed as dist
from torch.optim import Adam

import random
import numpy as np
import sacrebleu

torch.manual_seed(621311)

def lmap(f, x):
    """list(map(f, x))"""
    return list(map(f, x))

def label_smoothed_nll_loss(lprobs, target, epsilon, ignore_index=0):
    """From fairseq. This returns the label smoothed loss."""
    if target.dim() == lprobs.dim() - 1:
        target = target.unsqueeze(-1)
    nll_loss = -lprobs.gather(dim=-1, index=target)
    smooth_loss = -lprobs.sum(dim=-1, keepdim=True)
    if ignore_index is not None:
        pad_mask = target.eq(ignore_index)
        nll_loss.masked_fill_(pad_mask, 0.0)
        smooth_loss.masked_fill_(pad_mask, 0.0)
    else:
        nll_loss = nll_loss.squeeze(-1)
        smooth_loss = smooth_loss.squeeze(-1)

    nll_loss = nll_loss.mean()
    smooth_loss = smooth_loss.mean()
    eps_i = epsilon / lprobs.size(-1)
    loss = (1.0 - epsilon) * nll_loss + eps_i * smooth_loss
    return loss, nll_loss


def get_sacrebleu(refs, hyp):
    """Returns sacrebleu score."""
    bleu = sacrebleu.corpus_bleu(hyp, refs)
    return bleu.score

def assert_all_frozen(model):
    """Checks if frozen parameters are all linked to each other or not. Ensures no disjoint components of graphs."""
    model_grads: List[bool] = list(grad_status(model))
    n_require_grad = sum(lmap(int, model_grads))
    npars = len(model_grads)
    assert not any(model_grads), f"{n_require_grad/npars:.1%} of {npars} weights require grad"

def grad_status(model):
    """Checks whether the parameter needs gradient or not. Part of asserting that the correct parts of the model are frozen."""
    return (par.requires_grad for par in model.parameters())


def freeze_params(model):
    """Set requires_grad=False for each of model.parameters()"""
    for par in model.parameters():
        par.requires_grad = False

def freeze_embeds(model):
    """Freeze token embeddings and positional embeddings for bart, just token embeddings for t5."""
    try:
        freeze_params(model.model.shared)
        for d in [model.model.encoder, model.model.decoder]:
            freeze_params(d.embed_positions)
            freeze_params(d.embed_tokens)
    except AttributeError:
        freeze_params(model.shared)
        for d in [model.encoder, model.decoder]:
            freeze_params(d.embed_tokens)

def generate_batches_eval(tok, args):
    """Generates the source sentences for the dev set."""
    src_file = open(args.dev_src)
    curr_batch_count = 0
    encoder_input_batch = []
    max_src_sent_len = 0

    for src_line in src_file:
        start = time.time()
        src_sent = src_line
        lang = "<2"+args.slang+">"
        src_sent_split = src_sent.split(" ")
        sent_len = len(src_sent_split)
        if sent_len <1 or sent_len > 256:
            src_sent = " ".join(src_sent_split[:256])
        iids = tok(src_sent + " </s> " + lang, add_special_tokens=False, return_tensors="pt").input_ids
        curr_src_sent_len = len(iids[0])

        if curr_src_sent_len > max_src_sent_len:
            max_src_sent_len = curr_src_sent_len

        encoder_input_batch.append(src_sent + " </s> " + lang)
        curr_batch_count += 1
        if curr_batch_count == args.dev_batch_size:
            input_ids = tok(encoder_input_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_src_sent_len).input_ids
            input_masks = (input_ids != tok.pad_token_id).int()
            end = time.time()
            yield input_ids, input_masks
            curr_batch_count = 0
            encoder_input_batch = []
            max_src_sent_len = 0

    if len(encoder_input_batch) != 0:
        input_ids = tok(encoder_input_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_src_sent_len).input_ids
        input_masks = (input_ids != tok.pad_token_id).int()
        yield input_ids, input_masks


def yield_corpus_indefinitely(corpus):
    """This shuffles the corpus at the beginning of each epoch and returns sentences indefinitely."""
    epoch_counter = 0
    while True:
        print("Shuffling corpus!")
        random.shuffle(corpus)
        for src_line, tgt_line in corpus:
            yield src_line, tgt_line
        
        epoch_counter += 1
        print("Finished epoch", epoch_counter)
    return None, None


def generate_batches(tok, args):
    """Generates the source, target and source attention masks for the training set."""
    batch_count = 0
    src_file = open(args.train_src)
    tgt_file = open(args.train_tgt)
    corpus = [(src_line, tgt_line) for src_line, tgt_line in zip(src_file, tgt_file)]
    epoch_counter = 0
    corpus_gen = yield_corpus_indefinitely(corpus)
    while batch_count != args.num_batches:
        curr_batch_count = 0
        encoder_input_batch = []
        decoder_input_batch = []
        decoder_label_batch = []
        batch_count += 1
        max_src_sent_len = 0
        max_tgt_sent_len = 0
        start = time.time()
        for src_sent, tgt_sent in corpus_gen:
            slang = "<2"+args.slang+">"
            tlang = "<2"+args.tlang+">"
            src_sent_split = src_sent.split(" ")
            tgt_sent_split = tgt_sent.split(" ")
            tgt_sent_len = len(tgt_sent_split)
            src_sent_len = len(src_sent_split)
            if src_sent_len <=1 or src_sent_len >= 100 or tgt_sent_len <=1 or tgt_sent_len >= 100:
                continue
            iids = tok(src_sent + " </s> " + slang, add_special_tokens=False, return_tensors="pt").input_ids
            curr_src_sent_len = len(iids[0])
            
            iids = tok(tlang + " " + tgt_sent, add_special_tokens=False, return_tensors="pt").input_ids
            curr_tgt_sent_len = len(iids[0])
            if curr_src_sent_len <= 1 or curr_src_sent_len >= 100 or curr_tgt_sent_len <= 1 or curr_tgt_sent_len >= 100:
                continue
            if curr_src_sent_len > max_src_sent_len:
                max_src_sent_len = curr_src_sent_len
            
            if curr_tgt_sent_len > max_tgt_sent_len:
                max_tgt_sent_len = curr_tgt_sent_len
            
            encoder_input_batch.append(src_sent + " </s> " + slang)
            decoder_input_batch.append(tlang + " " + tgt_sent)
            decoder_label_batch.append(tgt_sent + " </s>")
            curr_batch_count += curr_tgt_sent_len
            if curr_batch_count > args.batch_size:
                break
        input_ids = tok(encoder_input_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_src_sent_len).input_ids
        input_masks = (input_ids != tok.pad_token_id).int()
        decoder_input_ids = tok(decoder_input_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_tgt_sent_len).input_ids
        labels = tok(decoder_label_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_tgt_sent_len).input_ids
        end = time.time()
        yield input_ids, input_masks, decoder_input_ids, labels
   
def init_weights(module, in_features, out_features):
    """Method to initialize model weights. Not used for now but might be used in the future. Tries to mimic t2t initialization."""
    if isinstance(module, nn.Linear):
        init_std = (3.0/(in_features+out_features))**(0.5)
        module.weight.data.normal_(mean=0.0, std=init_std)
        if module.bias is not None:
            module.bias.data.zero_()
    elif isinstance(module, nn.Embedding):
        init_std = (3.0/(out_features))**(0.5)
        module.weight.data.normal_(mean=0.0, std=init_std)
        if module.padding_idx is not None:
            module.weight.data[module.padding_idx].zero_()

def model_create_load_run_save(gpu, args):
    """The main function which does the magic. Should be split into multiple parts in the future."""
    rank = args.nr * args.gpus + gpu
    if not args.single_gpu:
        dist.init_process_group(backend='nccl', init_method='env://', world_size=args.world_size, rank=rank)
    
    tok = AutoTokenizer.from_pretrained(args.tokenizer_name_or_path, do_lower_case=False, use_fast=False, keep_accents=True)

    files = {"as": "data/as/as.txt", "bn": "data/bn/bn.txt", "en": "data/en/en.txt", "gu": "data/gu/gu.txt", "hi": "data/hi/hi.txt", "kn": "data/kn/kn.txt", "ml": "data/ml/ml.txt", "mr": "data/mr/mr.txt", "or": "data/or/or.txt", "pa": "data/pa/pa.txt", "ta": "data/ta/ta.txt", "te": "data/te/te.txt"}  ## Get this from command line
    
    special_tokens_dict = {'additional_special_tokens': ["<s>", "</s>"] + ["<2"+lang+">" for lang in files.keys()] + ["<2"+args.slang+">", "<2"+args.tlang+">"]}
    num_added_toks = tok.add_special_tokens(special_tokens_dict)

    print("Tokenizer is:", tok)
    
    if args.single_gpu:
        print(f"Running checkpoint example on rank {rank}.")
    else:
        print(f"Running DDP checkpoint example on rank {rank}.")
    if args.fp16:
        print("We will do fp16 training")
        scaler = torch.cuda.amp.GradScaler()
    else:
        print("We will do fp32 training")
    
    config = MBartConfig(vocab_size=len(tok), encoder_layers=args.encoder_layers, decoder_layers=args.decoder_layers, dropout=args.dropout, attention_dropout=args.attention_dropout, activation_dropout=args.activation_dropout, encoder_attention_heads=args.encoder_attention_heads, decoder_attention_heads=args.decoder_attention_heads, encoder_ffn_dim=args.encoder_ffn_dim, decoder_ffn_dim=args.decoder_ffn_dim, d_model=args.d_model, add_final_layer_norm=args.add_final_layer_norm, normalize_before=args.normalize_before, normalize_embedding=args.normalize_embedding, scale_embedding=args.scale_embedding, pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"]).input_ids[0][1], bos_token_id=tok(["<s>"]).input_ids[0][1], static_position_embeddings=True)
    model = MBartForConditionalGeneration(config)
    model.train()

    torch.cuda.set_device(gpu)
    
    if args.freeze_embeddings:
        print("Freezing embeddings")
        freeze_embeds(model)
    if args.freeze_encoder:
        print("Freezing encoder")
        freeze_params(model.get_encoder())
        assert_all_frozen(model.get_encoder())

    model.cuda(gpu)

    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.lr, eps=1e-09)
    
    if args.single_gpu:
        pass
    else:
        model = DistributedDataParallel(model, device_ids=[gpu], output_device=gpu)
    scheduler = get_linear_schedule_with_warmup(optimizer, args.warmup_steps, args.num_batches*args.world_size)
    
    while scheduler.get_lr()[0] < 1e-7:
        scheduler.step()
    print("Initial LR is:", scheduler.get_lr()[0])
    
    if args.pretrained_bilingual_model == "" and args.pretrained_model != "":
        print("Loading a pretrained mbart model")
        if args.single_gpu:
            pass
        else:
            dist.barrier()
        # configure map_location properly
        map_location = {'cuda:%d' % 0: 'cuda:%d' % rank}
        checkpoint_dict = torch.load(args.pretrained_model, map_location=map_location)
        if type(checkpoint_dict) == dict:
            model.load_state_dict(checkpoint_dict['model'])
        else:
            model.load_state_dict(checkpoint_dict)
    elif args.pretrained_bilingual_model != "":
        print("Loading a previous checkpoint")
        if args.single_gpu:
            pass
        else:
            dist.barrier()
            # configure map_location properly
        map_location = {'cuda:%d' % 0: 'cuda:%d' % rank}
        checkpoint_dict = torch.load(CHECKPOINT_PATH, map_location=map_location)
        if type(checkpoint_dict) == dict:
            model.load_state_dict(checkpoint_dict['model'])
            optimizer.load_state_dict(checkpoint_dict['optimizer'])
            scheduler.load_state_dict(checkpoint_dict['scheduler'])
            ctr = checkpoint_dict['ctr']
        else:
            model.load_state_dict(checkpoint_dict)
            ctr = 0
    else:
        print("Training from scratch")
        ctr = 0
        
    print("Using label smoothing of", args.label_smoothing)
    print("Using gradient clipping norm of", args.max_gradient_clip_value)
    #config.save_pretrained(args.fine_tuned_model+"/config")
    ctr = 0
    bleu_history = []
    max_sbleu = 0
    max_sbleu_step = 0
    curr_eval_step = 0
    annealing_attempt = 0
    for input_ids, input_masks, decoder_input_ids, labels in generate_batches(tok, args):
        start = time.time()
        if ctr % 1000 == 0:
            CHECKPOINT_PATH = args.fine_tuned_model
            if rank == 0:
                print("Running eval on dev set")
                refs = [[refline.strip() for refline in open(args.dev_tgt)]]
                hyp = []
                if args.single_gpu:
                    model.eval()
                else:
                    model.module.eval()
                    
                for dev_input_ids, dev_input_masks in generate_batches_eval(tok, args): #infinite_same_sentence(10000):
                    start = time.time()
                    if args.single_gpu:
                        translations = model.generate(dev_input_ids.to(gpu), use_cache=True, num_beams=1, max_length=int(len(input_ids[0])*1.5), early_stopping=True, attention_mask=dev_input_masks.to(gpu), pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"]).input_ids[0][1], decoder_start_token_id=tok(["<2"+args.tlang+">"]).input_ids[0][1], bos_token_id=tok(["<s>"]).input_ids[0][1])
                    else:
                        translations = model.module.generate(dev_input_ids.to(gpu), use_cache=True, num_beams=1, max_length=int(len(input_ids[0])*1.5), early_stopping=True, attention_mask=dev_input_masks.to(gpu), pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"]).input_ids[0][1], decoder_start_token_id=tok(["<2"+args.tlang+">"]).input_ids[0][1], bos_token_id=tok(["<s>"]).input_ids[0][1])
                    
                    for translation in translations:
                        translation  = tok.decode(translation, skip_special_tokens=True, clean_up_tokenization_spaces=False) 
                        hyp.append(translation)
                sbleu = get_sacrebleu(refs, hyp)
                print("BLEU score using sacrebleu after", ctr, "iterations is:", sbleu)
                if sbleu > max_sbleu:
                    max_sbleu = sbleu
                    max_sbleu_step = curr_eval_step
                    print("New peak reached. Saving.")
                    checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr}
                    torch.save(checkpoint_dict, CHECKPOINT_PATH+".best_dev_bleu."+str(ctr))
                    if args.single_gpu:
                        torch.save(model.state_dict(), CHECKPOINT_PATH+".best_dev_bleu."+str(ctr)+".pure_model") ## Pure model with ddp markers and no optimizer info.
                    else:
                        torch.save(model.module.state_dict(), CHECKPOINT_PATH+".best_dev_bleu."+str(ctr)+".pure_model") ## Pure model without any ddp markers or optimizer info.
                if curr_eval_step - max_sbleu_step > (args.early_stop_checkpoints + annealing_attempt*args.additional_early_stop_checkpoints_per_anneal_step):
                    if annealing_attempt < args.max_annealing_attempts:
                        annealing_attempt += 1
                        curr_lr = scheduler.get_lr()[0]
                        print("LR before annealing is:", curr_lr)
                        while scheduler.get_lr()[0] > (curr_lr/args.learning_rate_scaling):
                            scheduler.step()
                        print("LR after annealing is:", scheduler.get_lr()[0])
                    
                    else:
                        print("We have seemingly converged as BLEU failed to increase for the following number of checkpoints:", args.early_stop_checkpoints+annealing_attempt*args.additional_early_stop_checkpoints_per_anneal_step, ". You may want to consider increasing the number of tolerance steps, doing additional annealing or having a lower peak learning rate or something else.")
                        print("Terminating training")
                        break
                bleu_history.append(sbleu)
                curr_eval_step += 1
                
                if args.single_gpu:
                    model.train()
                else:
                    model.module.train()
                
                print("Saving the model")
                # All processes should see same parameters as they all start from same
                # random parameters and gradients are synchronized in backward passes.
                # Therefore, saving it in one process is sufficient.
                checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr}
                torch.save(checkpoint_dict, CHECKPOINT_PATH)
                

            # Use a barrier() to make sure that process 1 loads the model after process
            # 0 saves it.
            if args.single_gpu:
                pass
            else:
                dist.barrier()
            # configure map_location properly
            map_location = {'cuda:%d' % 0: 'cuda:%d' % rank}
            checkpoint_dict = torch.load(CHECKPOINT_PATH, map_location=map_location)
            model.load_state_dict(checkpoint_dict['model'])
            optimizer.load_state_dict(checkpoint_dict['optimizer']) ## Dubious
            scheduler.load_state_dict(checkpoint_dict['scheduler']) ## Dubious
            

        try:
            if args.fp16:
                with torch.cuda.amp.autocast():
                    mod_compute = model(input_ids=input_ids.to(gpu), attention_mask=input_masks.to(gpu) ,decoder_input_ids=decoder_input_ids.to(gpu), labels=labels.to(gpu))
                    logits = mod_compute[1]
                    if args.label_smoothing == 0.0:
                        loss = mod_compute[0]
                    else:
                        lprobs = torch.nn.functional.log_softmax(logits, dim=-1)
                        loss, nll_loss = label_smoothed_nll_loss(
                            lprobs, labels.to(gpu), args.label_smoothing, ignore_index=tok.pad_token_id
                        )
            else:
                mod_compute = model(input_ids=input_ids.to(gpu), attention_mask=input_masks.to(gpu) ,decoder_input_ids=decoder_input_ids.to(gpu), labels=labels.to(gpu))
                logits = mod_compute[1]
                if args.label_smoothing == 0.0:
                    loss = mod_compute[0]
                else:
                    lprobs = torch.nn.functional.log_softmax(logits, dim=-1)
                    loss, nll_loss = label_smoothed_nll_loss(
                        lprobs, labels.to(gpu), args.label_smoothing, ignore_index=tok.pad_token_id
                    )
        except Exception as e:
            print("NAN loss was computed or something messed up")
            print(e)
            sys.stdout.flush()
        optimizer.zero_grad()
        if args.fp16:
            scaler.scale(loss).backward()
        else:
            pass
        lv = loss.detach().cpu().numpy()
        if ctr % 10 == 0 and rank == 0:
            print(ctr, lv)
            sys.stdout.flush()
        if args.fp16:
            if args.max_gradient_clip_value != 0.0:
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_gradient_clip_value)
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            if args.max_gradient_clip_value != 0.0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_gradient_clip_value)
            optimizer.step()
        scheduler.step()
        end = time.time()
        ctr += 1
    
    CHECKPOINT_PATH = args.fine_tuned_model
    print("Saving the model after the final step")
    # All processes should see same parameters as they all start from same
    # random parameters and gradients are synchronized in backward passes.
    # Therefore, saving it in one process is sufficient.
    print("The best bleu was:", max_sbleu)
    print("The corresponding step was:", max_sbleu_step*1000)
    checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr}
    torch.save(checkpoint_dict, CHECKPOINT_PATH)
    if args.single_gpu:
        torch.save(model.state_dict(), CHECKPOINT_PATH+".pure_model") ## Pure model with ddp markers and no optimizer info
    else:
        torch.save(model.module.state_dict(), CHECKPOINT_PATH+".pure_model") ## Pure model without any ddp markers or optimizer info.
    if args.single_gpu:
        pass
    else:
        dist.destroy_process_group()
    

def run_demo():
    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--nodes', default=1,
                        type=int, metavar='N')
    parser.add_argument('-g', '--gpus', default=1, type=int,
                        help='number of gpus per node')
    parser.add_argument('-nr', '--nr', default=0, type=int,
                        help='ranking within the nodes')
    parser.add_argument('-a', '--ipaddr', default='localhost', type=str, 
                        help='IP address of the main node')
    parser.add_argument('-p', '--port', default='26023', type=str, 
                        help='Port main node')
    parser.add_argument('--freeze_embeddings', action='store_true', 
                        help='Should freeze embeddings during fine tuning?')
    parser.add_argument('--freeze_encoder', action='store_true', 
                        help='Should we freeze encoder during fine tuning?')
    parser.add_argument('--add_final_layer_norm', action='store_true', 
                        help='Should we add a final layer norm?')
    parser.add_argument('--normalize_before', action='store_true', 
                        help='Should we normalize before doing attention?')
    parser.add_argument('--normalize_embedding', action='store_true', 
                        help='Should we normalize embeddings?')
    parser.add_argument('--scale_embedding', action='store_true', 
                        help='Should we scale embeddings?')
    parser.add_argument('--mnmt', action='store_true', 
                        help='Are we training MNMT models? If so then the datagen will be slightly tweaked. We will also expect that training and development files will be comma separated when passed as arguments. The slang and tlang markers will also be comma separated and will follow the order of these files.')
    parser.add_argument('--encoder_layers', default=6, type=int, help="The value for number of encoder layers")
    parser.add_argument('--decoder_layers', default=6, type=int, help="The value for number of decoder layers")
    parser.add_argument('--label_smoothing', default=0.1, type=float, help="The value for label smoothing")
    parser.add_argument('--weight_decay', default=0.0001, type=float, help="The value for weight decay")
    parser.add_argument('--lr', default=7e-4, type=float, help="The value for the learning rate")
    parser.add_argument('--dropout', default=0.1, type=float, help="The value for embedding dropout")
    parser.add_argument('--attention_dropout', default=0.1, type=float, help="The value for attention dropout")
    parser.add_argument('--activation_dropout', default=0.1, type=float, help="The value for activation dropout")
    parser.add_argument('--encoder_attention_heads', default=8, type=int, help="The value for number of encoder attention heads")
    parser.add_argument('--decoder_attention_heads', default=8, type=int, help="The value for number of decoder attention heads")
    parser.add_argument('--decoder_ffn_dim', default=2048, type=int, help="The value for decoder ff hidden dim")
    parser.add_argument('--encoder_ffn_dim', default=2048, type=int, help="The value for encoder ff hidden dim")
    parser.add_argument('--d_model', default=512, type=int, help="The value for model hidden size")
    parser.add_argument('--max_gradient_clip_value', default=0.0, type=float, help="The max value for gradient norm value")

    parser.add_argument('--pretrained_model', default='', type=str, 
                        help='Path to the pretrained model')
    parser.add_argument('--pretrained_bilingual_model', default='', type=str, 
                        help='Path to the pretrained bilingual model. Use this if you want to continue training a bilingual model.')
    parser.add_argument('-m', '--fine_tuned_model', default='pytorch.bin', type=str, 
                        help='Path to save the fine tuned model')
    parser.add_argument('--warmup_steps', default=16000, type=int,
                        help='Scheduler warmup steps')
    parser.add_argument('--batch_size', default=1024, type=int, 
                        help='Train batch sizes in tokens')
    parser.add_argument('--dev_batch_size', default=1024, type=int, 
                        help='Dev batch sizes in lines')
    parser.add_argument('--early_stop_checkpoints', default=10, type=int, 
                        help='Number of checkpoints to wait to see if BLEU increases.')
    parser.add_argument('--learning_rate_scaling', default=2, type=int, 
                        help='How much should the LR be divided by during annealing?. Set num_batches to a larger value or else you will see lr go to zero too soon.')
    parser.add_argument('--max_annealing_attempts', default=2, type=int, 
                        help='Number of times LR should be annealed.')
    parser.add_argument('--additional_early_stop_checkpoints_per_anneal_step', default=5, type=int, 
                        help='How many additional checkpoints should we wait till declaring convergence? This will be multiplied with the annealing step number.')
    parser.add_argument('--num_batches', default=1000000, type=int, 
                        help='Number of batches to train on')
    parser.add_argument('--slang', default='en', type=str, 
                        help='Source language')
    parser.add_argument('--tokenizer_name_or_path', default='ai4bharat/indic-bert', type=str, 
                        help='Name of or path to the pre-trained indic language tokenizer')
    parser.add_argument('--tlang', default='hi', type=str, 
                        help='Target language')
    parser.add_argument('--train_src', default='', type=str, 
                        help='Source language training sentences')
    parser.add_argument('--train_tgt', default='', type=str, 
                        help='Target language training sentences')
    parser.add_argument('--dev_src', default='', type=str, 
                        help='Source language development sentences')
    parser.add_argument('--dev_tgt', default='', type=str, 
                        help='Target language development sentences')
    parser.add_argument('--fp16', action='store_true', 
                        help='Should we use fp16 training?')
    parser.add_argument('--single_gpu', action='store_true', 
                        help='Should we use single gpu training?')
    args = parser.parse_args()
    print("IP address is", args.ipaddr)
    #########################################################
    args.world_size = args.gpus * args.nodes                #
    os.environ['MASTER_ADDR'] = args.ipaddr              #
    os.environ['MASTER_PORT'] = args.port                      #
    if args.single_gpu:
        print("Non ddp model being trained")
        model_create_load_run_save(0, args)#
    else:
        mp.spawn(model_create_load_run_save, nprocs=args.gpus, args=(args,))         #
    #########################################################
    
if __name__ == "__main__":
    run_demo()
    
    
    
## Defunct code

    #print(model)
#     if args.pretrained_bilingual_model == "" and args.pretrained_model == "":
#         print("Manual initialization")
#         for module in model.modules():
#             if isinstance(module, nn.Linear):
#                 print("Initializing", module)
#                 init_weights(module, module.in_features, module.out_features)
#             if isinstance(module, torch.nn.Embedding):
# #                 print(type(module))
# #                 if isinstance(module, transformers.models.mbart.modeling_mbart.MBartLearnedPositionalEmbedding):
# #                     print("Not initializing", module)
# #                 else:
#                 print("Initializing", module)
#                 init_weights(module, len(tok), args.d_model) ## Might need modification
            