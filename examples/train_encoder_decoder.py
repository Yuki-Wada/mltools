"""
Train an Encoder-Decoder model.
"""
import os
import argparse
import logging
import json
from collections import OrderedDict
from tqdm import tqdm
import numpy as np

import torch

from mltools.utils import set_seed, set_logger, dump_json, get_date_str
# from mltools.dataset.japanese_english_bilingual_corpus import \
#     BilingualDataSet as DataSet, BilingualDataLoader as DataLoader
from mltools.dataset.tanaka_corpus import \
    TanakaCorpusDataSet as DataSet, TanakaCorpusDataLoader as DataLoader
from mltools.model.encoder_decoder import decoder_loss, calc_bleu_scores, \
    NaiveSeq2Seq, Seq2SeqWithGlobalAttention, TransformerEncoderDecoder
from mltools.optimizer.utils import get_torch_optimizer, get_torch_lr_scheduler
from mltools.metric.metric_manager import MetricManager

logger = logging.getLogger(__name__)

def get_args():
    parser = argparse.ArgumentParser()

    parser.add_argument('--gpu_id', type=int, default=-1)

    parser.add_argument('--train_data', nargs='+', required=True)
    parser.add_argument('--valid_data', nargs='+', required=True)
    parser.add_argument('--lang', default='ja_to_en')
    parser.add_argument('--output_dir_format', default='.')
    parser.add_argument('--model_name_format', default='epoch-{epoch}.hdf5')
    parser.add_argument('--preprocessor', default='preprocessor.bin')

    parser.add_argument('--model', default='naive')
    parser.add_argument('--embedding_dimension', dest='emb_dim', type=int, default=400)
    parser.add_argument('--hidden_dimension', dest='hidden_dim', type=int, default=200)
    parser.add_argument('--bidirectional', action='store_true')
    parser.add_argument('--model_params')
    parser.add_argument('--initial_weight')

    parser.add_argument('--optimizer', dest='optim', default='sgd')
    parser.add_argument('--learning_rate', '-lr', dest='lr', type=float, default=1e-3)
    parser.add_argument('--weight_decay', '-wd', type=float, default=1e-5)
    parser.add_argument('--momentum', type=float, default=0.0)
    parser.add_argument('--nesterov', action='store_true')
    parser.add_argument('--clipvalue', type=float)
    parser.add_argument('--clipnorm', type=float)

    parser.add_argument('--lr_scheduler', default='constant')
    parser.add_argument('--lr_decay', type=float, default=1e-1)
    parser.add_argument('--lr_steps', nargs='+', type=float, default=[0.1, 0.5, 0.75, 0.9])
    parser.add_argument('--min_lr', type=float, default=1e-5)

    parser.add_argument('--epochs', type=int, default=20, help='epoch count')
    parser.add_argument('--mb_size', type=int, default=32, help='minibatch size')

    parser.add_argument('--decoding_type', default='random_choice')
    parser.add_argument('--breadth_len', type=int, default=8)

    parser.add_argument('--seed', type=int, help='random seed for initialization')

    args = parser.parse_args()

    return args

def get_decoding_params(args, preprocessor):
    decoding_params = {
        'decoding_type': args.decoding_type,
        'begin_of_encode_index': preprocessor.en_begin_of_encode_index,
    }
    if decoding_params['decoding_type'] == 'beam_search':
        decoding_params['breadth_len'] = args.breadth_len

    return decoding_params

def get_model_params(args):
    if args.model == 'naive' or args.model == 'global_attention':
        return {
            'model': args.model,
            'emb_dim': args.emb_dim,
            'hidden_dim': args.hidden_dim,
            'bidirectional': args.bidirectional,
            'gpu_id': args.gpu_id,
        }
    if args.model == 'transformer':
        return {
            'model': args.model,
            'model_dim': args.emb_dim,
            'head_count': 4,
            'feed_forward_hidden_dim': 512,
            'encoder_block_count': 6,
            'decoder_block_count': 6,
            'gpu_id': args.gpu_id,
        }
    raise ValueError('The model {} is not supported.'.format(args.model))

def get_model(model_params):
    if model_params['model'] == 'naive':
        return NaiveSeq2Seq(
            model_params['encoder_vocab_count'],
            model_params['decoder_vocab_count'],
            model_params['emb_dim'],
            model_params['hidden_dim'],
            model_params['bidirectional'],
            model_params['gpu_id'],
        )
    if model_params['model'] == 'global_attention':
        return Seq2SeqWithGlobalAttention(
            model_params['encoder_vocab_count'],
            model_params['decoder_vocab_count'],
            model_params['emb_dim'],
            model_params['hidden_dim'],
            model_params['bidirectional'],
            model_params['gpu_id'],
        )
    if model_params['model'] == 'transformer':
        return TransformerEncoderDecoder(
            encoder_vocab_count=model_params['encoder_vocab_count'],
            decoder_vocab_count=model_params['decoder_vocab_count'],
            model_dim=model_params['model_dim'],
            head_count=model_params['head_count'],
            feed_forward_hidden_dim=model_params['feed_forward_hidden_dim'],
            encoder_block_count=model_params['encoder_block_count'],
            decoder_block_count=model_params['decoder_block_count'],
            gpu_id=model_params['gpu_id'],
        )

    raise ValueError('The model {} is not supported.'.format(model_params['model']))

def get_optimizer_params(args):
    optimizer_params = {}
    optimizer_params['type'] = args.optim

    optimizer_params['kwargs'] = {}
    if args.clipvalue:
        optimizer_params['kwargs']['clipvalue'] = args.clipvalue
    if args.clipnorm:
        optimizer_params['kwargs']['clipnorm'] = args.clipnorm

    if args.optim == 'sgd':
        optimizer_params['kwargs']['lr'] = args.lr
        optimizer_params['kwargs']['decay'] = args.lr * args.weight_decay
        optimizer_params['kwargs']['momentum'] = args.momentum
        optimizer_params['kwargs']['nesterov'] = args.nesterov

        return optimizer_params

    if args.optim == 'adadelta':
        optimizer_params['kwargs']['decay'] = args.weight_decay

        return optimizer_params

    if args.optim == 'adam':
        optimizer_params['kwargs']['lr'] = args.lr
        optimizer_params['kwargs']['decay'] = args.weight_decay

        return optimizer_params

    raise ValueError('The optimizer {} is not supported.'.format(args.optim))

def get_lr_scheduler_params(args, train_data_loader):
    lr_scheduler_params = {}
    lr_scheduler_params['type'] = args.lr_scheduler
    lr_scheduler_params['kwargs'] = {}

    if args.lr_scheduler == 'constant':
        return lr_scheduler_params

    if args.lr_scheduler == 'multi_step':
        lr_scheduler_params['kwargs']['milestones'] = [
            int(args.epochs * step) for step in args.lr_steps
        ]
        lr_scheduler_params['kwargs']['gamma'] = args.lr_decay

        return lr_scheduler_params

    if args.lr_scheduler == 'cyclic':
        lr_scheduler_params['kwargs']['base_lr'] = args.min_lr
        lr_scheduler_params['kwargs']['max_lr'] = args.lr
        lr_scheduler_params['kwargs']['step_size_up'] = train_data_loader.iter_count
        lr_scheduler_params['kwargs']['mode'] = 'triangular'

        return lr_scheduler_params

    if args.lr_scheduler == 'cosine_annealing':
        lr_scheduler_params['kwargs']['T_max'] = train_data_loader.iter_count * 2
        lr_scheduler_params['kwargs']['eta_min'] = args.min_lr

        return lr_scheduler_params

    raise ValueError(
        'The learning rate scheduler {} is not supported.'.format(args.lr_scheduler))

def setup_output_dir(output_dir_path, args, model_params, optimizer_params, decoding_params):
    os.makedirs(output_dir_path, exist_ok=True)
    dump_json(args, os.path.join(output_dir_path, 'args.json'))
    dump_json(model_params, os.path.join(output_dir_path, 'model.json'))
    dump_json(optimizer_params, os.path.join(output_dir_path, 'optimizer.json'))
    dump_json(decoding_params, os.path.join(output_dir_path, 'decoding.json'))

def train_model(
        model,
        train_data_loader,
        optimizer,
        lr_scheduler,
        preprocessor,
        decoding_params,
        metric_manager,
        epoch,
    ):
    model.train()
    device = model.device

    train_loss_sum = 0.0
    train_data_count = 0
    bleu2_scores = []
    bleu4_scores = []
    with tqdm(total=len(train_data_loader), desc='Train') as pbar:
        for mb_inputs, mb_outputs in train_data_loader:
            mb_count = mb_inputs.shape[0]

            try:
                mb_inputs = torch.LongTensor(mb_inputs.transpose(1, 0)).to(device)
                mb_outputs = torch.LongTensor(mb_outputs.transpose(1, 0)).to(device)
                decoding_params['seq_len'] = mb_outputs.shape[0] - 1

                model.zero_grad()
                mb_preds, _ = model.inference(mb_inputs, decoding_params)
                mb_train_loss = decoder_loss(mb_outputs[1:], mb_preds)
                mb_train_loss.backward()
                optimizer.step()

                mb_train_loss = mb_train_loss.cpu().data.numpy()
                train_loss_sum += mb_train_loss * mb_count
                train_data_count += mb_count

                mb_outputs = mb_outputs.cpu().data.numpy().transpose(1, 0)[:, 1:]
                mb_predicted = np.argmax(mb_preds.cpu().data.numpy(), axis=2).transpose(1, 0)

                mb_bleu2_scores = calc_bleu_scores(
                    mb_outputs, mb_predicted, preprocessor.ja_eos_index, max_n=2)
                bleu2_scores += mb_bleu2_scores
                mb_bleu2_score = np.mean(mb_bleu2_scores)

                mb_bleu4_scores = calc_bleu_scores(
                    mb_outputs, mb_predicted, preprocessor.ja_eos_index, max_n=4)
                bleu4_scores += mb_bleu4_scores
                mb_bleu4_score = np.mean(mb_bleu4_scores)

            except RuntimeError as error:
                logger.error(str(error))
                mb_train_loss = np.nan

            finally:
                torch.cuda.empty_cache()

            pbar.update(mb_count)
            pbar.set_postfix(OrderedDict(
                loss=mb_train_loss,
                plex=np.exp(mb_train_loss),
                bleu2=mb_bleu2_score,
                bleu4=mb_bleu4_score,
            ))
            if lr_scheduler.step_type == 'iter':
                lr_scheduler.step()

        if lr_scheduler.step_type == 'epoch':
            lr_scheduler.step()

        train_loss = train_loss_sum / train_data_count
        logger.info('Train Loss: %f', train_loss)
        bleu2_score = np.mean(bleu2_scores)
        logger.info('BLEU2 Score: %f', bleu2_score)
        bleu4_score = np.mean(bleu4_scores)
        logger.info('BLEU4 Score: %f', bleu4_score)

        metric_manager.register_metric(train_loss, epoch, 'train', 'loss')
        metric_manager.register_metric(bleu2_score, epoch, 'train', 'bleu2')
        metric_manager.register_metric(bleu4_score, epoch, 'train', 'bleu4')

def evaluate_model(
        model,
        valid_data_loader,
        preprocessor,
        decoding_params,
        metric_manager,
        epoch,
    ):
    model.eval()
    device = model.device

    valid_loss_sum = 0.0
    valid_data_count = 0
    bleu2_scores = []
    bleu4_scores = []
    with tqdm(total=len(valid_data_loader), desc='Valid') as pbar:
        for mb_inputs, mb_outputs in valid_data_loader:
            mb_count = mb_inputs.shape[0]

            try:
                mb_inputs = torch.LongTensor(mb_inputs.transpose(1, 0)).to(device)
                mb_outputs = torch.LongTensor(mb_outputs.transpose(1, 0)).to(device)
                decoding_params['seq_len'] = mb_outputs.shape[0] - 1

                mb_preds, _ = model.inference(mb_inputs, decoding_params)
                mb_valid_loss = decoder_loss(mb_outputs[1:], mb_preds).cpu().data.numpy()

                mb_outputs = mb_outputs.cpu().data.numpy().transpose(1, 0)[:, 1:]
                mb_predicted = np.argmax(mb_preds.cpu().data.numpy(), axis=2).transpose(1, 0)

                valid_loss_sum += mb_valid_loss * mb_count
                valid_data_count += mb_count

                mb_bleu2_scores = calc_bleu_scores(
                    mb_outputs, mb_predicted, preprocessor.ja_eos_index, max_n=2)
                bleu2_scores += mb_bleu2_scores
                mb_bleu2_score = np.mean(mb_bleu2_scores)

                mb_bleu4_scores = calc_bleu_scores(
                    mb_outputs, mb_predicted, preprocessor.ja_eos_index, max_n=4)
                bleu4_scores += mb_bleu4_scores
                mb_bleu4_score = np.mean(mb_bleu4_scores)

            except RuntimeError as error:
                logger.error(str(error))
                mb_valid_loss = np.nan

            finally:
                torch.cuda.empty_cache()

            pbar.update(mb_count)
            pbar.set_postfix(OrderedDict(
                loss=mb_valid_loss,
                plex=np.exp(mb_valid_loss),
                bleu2=mb_bleu2_score,
                bleu4=mb_bleu4_score,
            ))

        valid_loss = valid_loss_sum / valid_data_count
        logger.info('Valid Loss: %f', valid_loss)
        bleu2_score = np.mean(bleu2_scores)
        logger.info('BLEU2 Score: %f', bleu2_score)
        bleu4_score = np.mean(bleu4_scores)
        logger.info('BLEU4 Score: %f', bleu4_score)

        metric_manager.register_metric(valid_loss, epoch, 'valid', 'loss')
        metric_manager.register_metric(bleu2_score, epoch, 'valid', 'bleu2')
        metric_manager.register_metric(bleu4_score, epoch, 'valid', 'bleu4')

    return valid_loss

def train_loop(
        train_data_loader,
        valid_data_loader,
        model,
        optimizer,
        lr_scheduler,
        preprocessor,
        decoding_params,
        epochs,
        output_dir_path,
        model_name_format,
        best_monitored_metric=None,
    ):
    # Train Model
    metric_manager = MetricManager(output_dir_path, epochs)
    for epoch in range(epochs):
        logger.info('Start Epoch %s', epoch + 1)

        train_model(
            model, train_data_loader, optimizer, lr_scheduler,
            preprocessor, decoding_params,
            metric_manager, epoch,
        )
        valid_loss = evaluate_model(
            model, valid_data_loader,
            preprocessor, decoding_params,
            metric_manager, epoch,
        )

        # Save
        metric_manager.plot_metric(
            'loss', 'Loss', os.path.join(output_dir_path, 'loss.png'))
        metric_manager.plot_metric(
            'bleu2', 'BLEU Score', os.path.join(output_dir_path, 'bleu2.png'))
        metric_manager.plot_metric(
            'bleu4', 'BLEU Score', os.path.join(output_dir_path, 'bleu4.png'))
        metric_manager.save_score()

        monitored_metric = - valid_loss
        if best_monitored_metric is None or best_monitored_metric < monitored_metric:
            best_monitored_metric = monitored_metric
            logger.info('The current score is best.')

        if model_name_format:
            model_name = model_name_format.format(epoch=epoch + 1)
            logger.info('Save the model as %s', model_name)

            device = model.device
            torch.save(model.to('cpu').state_dict(), os.path.join(output_dir_path, model_name))
            model.to(device)

    return best_monitored_metric

def run():
    set_logger()
    args = get_args()
    set_seed(args.seed)

    train_data_set = DataSet(is_training=True)
    train_data_set.input_data(args.train_data)
    train_data_loader = DataLoader(train_data_set, args.mb_size)
    preprocessor = train_data_set.preprocessor

    valid_data_set = DataSet(is_training=False, preprocessor=preprocessor)
    valid_data_set.input_data(args.valid_data)
    valid_data_loader = DataLoader(valid_data_set, args.mb_size)

    model_params = get_model_params(args)
    optimizer_params = get_optimizer_params(args)
    lr_scheduler_params = get_lr_scheduler_params(args, train_data_loader)
    decoding_params = get_decoding_params(args, preprocessor)
    if args.lang == 'ja_to_en':
        model_params['encoder_vocab_count'] = train_data_set.ja_vocab_count
        model_params['decoder_vocab_count'] = train_data_set.en_vocab_count
    elif args.lang == 'en_to_ja':
        model_params['encoder_vocab_count'] = train_data_set.en_vocab_count
        model_params['decoder_vocab_count'] = train_data_set.ja_vocab_count

    output_dir_path = args.output_dir_format.format(date=get_date_str())
    setup_output_dir(
        output_dir_path, dict(args._get_kwargs()), #pylint: disable=protected-access
        model_params, optimizer_params, decoding_params)
    preprocessor.save(os.path.join(output_dir_path, args.preprocessor))

    # Set up Model and Optimizer
    if args.model_params:
        with open(args.model_params, 'r') as f:
            model_params = json.load(f)
    model = get_model(model_params)
    if args.initial_weight:
        model.load_state_dict(torch.load(args.initial_weight))
    optimizer = get_torch_optimizer(model.parameters(), optimizer_params)
    lr_scheduler = get_torch_lr_scheduler(optimizer, lr_scheduler_params)

    train_loop(
        train_data_loader=train_data_loader,
        valid_data_loader=valid_data_loader,
        model=model,
        optimizer=optimizer,
        lr_scheduler=lr_scheduler,
        preprocessor=preprocessor,
        decoding_params=decoding_params,
        epochs=args.epochs,
        output_dir_path=output_dir_path,
        model_name_format=args.model_name_format,
    )

if __name__ == '__main__':
    run()
