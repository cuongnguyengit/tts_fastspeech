import torch
import torch.nn as nn
import numpy as np
import argparse
import os
import time
from torch.utils.data import DataLoader

from fastspeech1.model_fs import FastSpeech
from fastspeech1.loss_fs import DNNLoss
from fastspeech1.eval_fs import evaluate
from dataset.dataset_fs import BufferDataset
from dataset.dataset_fs import get_data_to_buffer, collate_fn_tensor
from fastspeech1.optimizer_fs import ScheduledOptim
from fastspeech1 import hp_fs1 as hp1

import hparams as hp
import utils


def main(args, device):
    hp.checkpoint_path = os.path.join(hp.root_path, args.name_task, "ckpt", hp.dataset)
    hp.synth_path = os.path.join(hp.root_path, args.name_task, "synth", hp.dataset)
    hp.eval_path = os.path.join(hp.root_path, args.name_task, "eval", hp.dataset)
    hp.log_path = os.path.join(hp.root_path, args.name_task, "log", hp.dataset)
    hp.test_path = os.path.join(hp.root_path, args.name_task, 'results')
    # Define model
    print("Use FastSpeech")
    model = nn.DataParallel(FastSpeech()).to(device)
    print("Model Has Been Defined")
    num_param = utils.get_param_num(model)
    print('Number of TTS Parameters:', num_param)
    # Get buffer
    print("Load data to buffer")
    buffer = get_data_to_buffer('train.txt')

    # Optimizer and loss
    optimizer = torch.optim.Adam(model.parameters(),
                                 betas=(0.9, 0.98),
                                 eps=1e-9)
    scheduled_optim = ScheduledOptim(optimizer,
                                     hp1.decoder_dim,
                                     hp1.n_warm_up_step,
                                     args.restore_step)
    fastspeech_loss = DNNLoss().to(device)
    print("Defined Optimizer and Loss Function.")

    # Load checkpoint if exists
    checkpoint_path = os.path.join(args.restore_path)
    try:
        checkpoint = torch.load(os.path.join(
            checkpoint_path, 'checkpoint_%d.pth.tar' % args.restore_step))
        model.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        print("\n---Model Restored at Step %d---\n" % args.restore_step)
    except:
        print("\n---Start New Training---\n")
        checkpoint_path = os.path.join(hp.checkpoint_path)
        if not os.path.exists(checkpoint_path):
            os.makedirs(checkpoint_path)

    # Init logger
    if not os.path.exists(hp.log_path):
        os.makedirs(hp.log_path)

    waveglow = None
    if hp.vocoder == 'waveglow':
        waveglow = utils.get_waveglow()

    # Get dataset
    dataset = BufferDataset(buffer)

    # Get Training Loader
    training_loader = DataLoader(dataset,
                                 batch_size=hp.batch_expand_size * hp.batch_size,
                                 shuffle=True,
                                 collate_fn=collate_fn_tensor,
                                 drop_last=True,
                                 num_workers=0)
    total_step = hp.epochs * len(training_loader) * hp.batch_expand_size

    # Define Some Information
    Time = np.array([])
    Start = time.perf_counter()

    # Training
    model = model.train()

    for epoch in range(hp.epochs):
        for i, batchs in enumerate(training_loader):
            # real batch start here
            for j, db in enumerate(batchs):
                print(len(batchs), len(db))
                start_time = time.perf_counter()

                current_step = i * hp.batch_expand_size + j + args.restore_step + \
                    epoch * len(training_loader) * hp.batch_expand_size + 1

                # Init
                scheduled_optim.zero_grad()

                # Get Data
                character = db["text"].long().to(device)
                mel_target = db["mel_target"].float().to(device)
                duration = db["duration"].int().to(device)
                mel_pos = db["mel_pos"].long().to(device)
                src_pos = db["src_pos"].long().to(device)
                max_mel_len = db["mel_max_len"]

                # Forward
                mel_output, mel_postnet_output, duration_predictor_output = model(character,
                                                                                  src_pos,
                                                                                  mel_pos=mel_pos,
                                                                                  mel_max_length=max_mel_len,
                                                                                  length_target=duration)

                # Cal Loss
                mel_loss, mel_postnet_loss, duration_loss = fastspeech_loss(mel_output,
                                                                            mel_postnet_output,
                                                                            duration_predictor_output,
                                                                            mel_target,
                                                                            duration)
                total_loss = mel_loss + mel_postnet_loss + duration_loss

                # Logger
                t_l = total_loss.item()
                m_l = mel_loss.item()
                m_p_l = mel_postnet_loss.item()
                d_l = duration_loss.item()

                with open(os.path.join(hp.log_path, "total_loss.txt"), "a") as f_total_loss:
                    f_total_loss.write(str(t_l)+"\n")

                with open(os.path.join(hp.log_path, "mel_loss.txt"), "a") as f_mel_loss:
                    f_mel_loss.write(str(m_l)+"\n")

                with open(os.path.join(hp.log_path, "mel_postnet_loss.txt"), "a") as f_mel_postnet_loss:
                    f_mel_postnet_loss.write(str(m_p_l)+"\n")

                with open(os.path.join(hp.log_path, "duration_loss.txt"), "a") as f_d_loss:
                    f_d_loss.write(str(d_l)+"\n")

                # Backward
                total_loss.backward()

                # Clipping gradients to avoid gradient explosion
                nn.utils.clip_grad_norm_(
                    model.parameters(), hp1.grad_clip_thresh)

                # Update weights
                if args.frozen_learning_rate:
                    scheduled_optim.step_and_update_lr_frozen(
                        args.learning_rate_frozen)
                else:
                    scheduled_optim.step_and_update_lr()

                # Print
                if current_step % hp.log_step == 0:
                    Now = time.perf_counter()

                    str1 = "Epoch [{}/{}], Step [{}/{}]:".format(
                        epoch+1, hp.epochs, current_step, total_step)
                    str2 = "Total Loss: {:.4f}, Mel Loss: {:.4f}, Mel PostNet Loss: {:.4f}, Duration Loss: {:.4f};".format(
                        t_l, m_l, m_p_l, d_l)
                    str3 = "Current Learning Rate is {:.6f}.".format(
                        scheduled_optim.get_learning_rate())
                    str4 = "Time Used: {:.3f}s, Estimated Time Remaining: {:.3f}s.".format(
                        (Now-Start), (total_step-current_step)*np.mean(Time))

                    print("\n" + str1)
                    print(str2)
                    print(str3)
                    print(str4)

                    with open(os.path.join(hp.log_path, "logger.txt"), "a") as f_logger:
                        f_logger.write(str1 + "\n")
                        f_logger.write(str2 + "\n")
                        f_logger.write(str3 + "\n")
                        f_logger.write(str4 + "\n")
                        f_logger.write("\n")

                if current_step % hp.save_step == 0:
                    torch.save({'model': model.state_dict(), 'optimizer': optimizer.state_dict(
                    )}, os.path.join(hp.checkpoint_path, 'checkpoint_%d.pth.tar' % current_step))
                    print("save model at step %d ..." % current_step)

                # if current_step % 10 == 0:
                #     model.eval()
                #     with torch.no_grad():
                #         t_l, d_l, mel_l, mel_p_l = evaluate(
                #             model, current_step, vocoder=waveglow)

                #         str0 = 'Validating'
                #         str1 = "\tEpoch [{}/{}], Step [{}/{}]:".format(
                #             epoch + 1, hp.epochs, current_step, total_step)
                #         str2 = "\tTotal Loss: {:.4f}, Mel Loss: {:.4f}, Mel PostNet Loss: {:.4f}, Duration Loss: {:.4f};".format(
                #             t_l, m_l, m_p_l, d_l)
                #         print(str0)
                #         print(str1)
                #         print(str2)
                #     model.train()

                end_time = time.perf_counter()
                Time = np.append(Time, end_time - start_time)
                if len(Time) == hp.clear_Time:
                    temp_value = np.mean(Time)
                    Time = np.delete(
                        Time, [i for i in range(len(Time))], axis=None)
                    Time = np.append(Time, temp_value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--restore_step', type=int, default=0)
    parser.add_argument('--frozen_learning_rate', type=bool, default=False)
    parser.add_argument("--learning_rate_frozen", type=float, default=1e-3)
    args = parser.parse_args()
    main(args)
