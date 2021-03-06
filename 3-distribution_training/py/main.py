# -*- coding: utf-8 -*-
import os
import time

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter
from torchvision import transforms

from args import opt
from loadDB import AnimeFaceDB
from model import resnet18
from train_val import train, validate
from utils import seed_everything, get_worker_init, init_distributed_mode, save_on_master

try:
    from apex import amp
except ImportError:
    amp = None


def load_data(args):
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    # 画像開いたところからtensorでNNに使えるようにするまでの変形
    train_transform = transforms.Compose([
        transforms.Resize(args.img_size, interpolation=2),  # リサイズ
        transforms.RandomCrop(args.crop_size),  # クロップ
        transforms.RandomHorizontalFlip(p=0.5),  # 左右反転
        transforms.ToTensor(),  # テンソル化
        normalize  # 標準化
    ])

    val_transform = transforms.Compose([
        transforms.Resize(args.img_size, interpolation=2),  # リサイズ
        transforms.CenterCrop(args.crop_size),
        transforms.ToTensor(),  # テンソル化
        normalize  # 標準化
    ])

    # AnimeFaceの学習用データ設定
    train_dataset = AnimeFaceDB(
        os.path.join(args.path2db, 'train'),
        transform=train_transform)

    # AnimeFaceの評価用データ設定
    val_dataset = AnimeFaceDB(
        os.path.join(args.path2db, 'val'),
        transform=val_transform)

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)
    else:
        train_sampler = torch.utils.data.RandomSampler(train_dataset)
        val_sampler = torch.utils.data.SequentialSampler(val_dataset)

    return train_dataset, val_dataset, train_sampler, val_sampler


def main():
    args = opt()
    print(args)
    seed_everything(args.seed)  # 乱数テーブル固定
    worker_init = get_worker_init()
    if args.apex and amp is None:
        raise RuntimeError("Failed to import apex. Please install apex from https://www.github.com/nvidia/apex "
                           "to enable mixed-precision training.")
    # フォルダが存在してなければ作る
    if not os.path.exists('weight'):
        os.mkdir('weight')

    init_distributed_mode(args)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')  # cpuとgpu自動選択 (pytorch0.4.0以降の書き方)
    ngpus_per_node = torch.cuda.device_count()
    writer = SummaryWriter(log_dir='log/AnimeFace')  # tensorboard用のwriter作成

    if args.distributed:
        args.batch_size = int(args.batch_size / ngpus_per_node)
        args.workers = int((args.workers + ngpus_per_node - 1) / ngpus_per_node)

    train_dataset, val_dataset, train_sampler, val_sampler = load_data(args)

    train_loader = torch.utils.data.DataLoader(
        dataset=train_dataset, batch_size=args.batch_size,
        sampler=train_sampler, num_workers=args.workers,
        pin_memory=True, drop_last=True,
        worker_init_fn=worker_init)

    val_loader = torch.utils.data.DataLoader(
        dataset=val_dataset, batch_size=args.batch_size,
        sampler=val_sampler, num_workers=args.workers,
        pin_memory=True, drop_last=False,
        worker_init_fn=worker_init)

    model = resnet18(pretrained=True, num_classes=args.num_classes).to(device)

    criterion = nn.CrossEntropyLoss().to(device)
    optimizer = optim.SGD(
        model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)  # 最適化方法定義

    iteration = 0  # 反復回数保存用

    if args.apex:
        model, optimizer = amp.initialize(
            model, optimizer,
            opt_level=args.apex_opt_level
        )

    model_without_ddp = model
    if args.distributed:
        model = nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module

    # 評価だけやる
    if args.evaluate:
        print("use pretrained model : %s" % args.resume)
        param = torch.load(args.resume, map_location=lambda storage, loc: storage)
        model_without_ddp.load_state_dict(param)
        model.to(device)  # gpu使うならcuda化
        validate(args, model, device, val_loader, criterion, writer, iteration)
        return

    scheduler = optim.lr_scheduler.MultiStepLR(
        optimizer, milestones=[int(0.5*args.epochs), int(0.75*args.epochs)], gamma=0.1)  # 学習率の軽減スケジュール

    best_acc = 0.0
    starttime = time.time()  # 実行時間計測(実時間)
    # 学習と評価
    for epoch in range(1, args.epochs + 1):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        train(args, model, device, train_loader, writer,
              criterion, optimizer, epoch, iteration, args.apex)
        iteration += len(train_loader)  # 1epoch終わった時のiterationを足す
        acc = validate(args, model, device, val_loader, criterion, writer, iteration)
        scheduler.step()  # 学習率のスケジューリング更新
        is_best = acc > best_acc
        best_acc1 = max(acc, best_acc)
        if is_best:
            saved_weight = 'weight/AnimeFace_resnet18_best.pth'
            save_on_master(model_without_ddp.state_dict(), saved_weight)
            model.to(device)

    writer.close()  # tensorboard用のwriter閉じる
    # 実行時間表示
    endtime = time.time()
    interval = endtime - starttime
    print('elapsed time = {0:d}h {1:d}m {2:d}s'.format(
        int(interval / 3600),
        int((interval % 3600) / 60),
        int((interval % 3600) % 60)))


if __name__ == '__main__':
    main()
