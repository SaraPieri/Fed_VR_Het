# coding=utf-8
from __future__ import absolute_import, division, print_function

import os
import argparse
import numpy as np
from copy import deepcopy
import torch
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from utils.data_utils import DatasetFLViT, create_dataset_and_evalmetrix
from utils.util import Partial_Client_Selection, valid, average_model, optimizer_to
from utils.start_config import initization_configure
from typing import List, Tuple, Union, OrderedDict
import wandb
import pandas as pd


def trainable_params(
    src: Union[OrderedDict[str, torch.Tensor], torch.nn.Module], requires_name=False
    ) -> Union[List[torch.Tensor], Tuple[List[str], List[torch.Tensor]]]:
    parameters = []
    keys = []
    if isinstance(src, OrderedDict):
        for name, param in src.items():
            if param.requires_grad:
                parameters.append(param)
                keys.append(name)
    elif isinstance(src, torch.nn.Module):
        for name, param in src.state_dict(keep_vars=True).items():
            if param.requires_grad:
                parameters.append(param)
                keys.append(name)

    if requires_name:
        return keys, parameters
    else:
        return parameters

def server_optimization_fun(args, global_params_dict):
    
    # Prepare optimizer for the server 
    if args.server_optimizer_type == 'sgd':
        nesterov = False if args.server_momentum == 0 else True
        server_optimizer = torch.optim.SGD(list(global_params_dict.values()), lr=args.server_learning_rate, momentum=args.server_momentum, nesterov=nesterov, weight_decay=args.server_weight_decay)
    elif args.server_optimizer_type == 'adam':
        server_optimizer = torch.optim.Adam(list(global_params_dict.values()), eps=1e-8, betas=(0.9, 0.999), lr=args.server_learning_rate, weight_decay=args.server_weight_decay)
    else:
        server_optimizer = torch.optim.Adam(list(global_params_dict.values()), eps=1e-8, betas=(0.9, 0.999), lr=args.server_learning_rate, weight_decay=args.server_weight_decay)
        print("===============Not implemented optimization type, we used default adamw optimizer ===============")
    
    print("============ Server Optimizer Created ============")
    return  server_optimizer

def aggregate_server(server_optimizer, global_params_dict, args, delta_cache, weight_cache):

    weights = torch.tensor(weight_cache, device=args.device) / sum(weight_cache)
    delta_list = [list(delta.values()) for delta in delta_cache]

    aggregated_delta = []
    for layer_delta in zip(*delta_list):
        aggregated_delta.append(
            torch.sum(
                torch.stack(layer_delta, dim=-1).to(args.device) * weights, dim=-1
            )
        )

    server_optimizer.zero_grad()

    for param, diff in zip(global_params_dict.values(), aggregated_delta):
        param.grad = diff.data

    server_optimizer.step()

def average_model(args, model_all, server_optimizer, global_params_dict, delta_cache, weight_cache):

    print('Calculate the model avg with Server otpimizer----')
    
    ## update optimizer state and pdated state to compute new global model 
    aggregate_server(server_optimizer, global_params_dict, args, delta_cache, weight_cache)
    
    print('Update each client model parameters----')

    for single_client in args.proxy_clients:
        tmp_params = dict(model_all[single_client].named_parameters())
        for name, param in global_params_dict.items():
            tmp_params[name].data.copy_(param.data)

def train(args, model):
    """ Train the model """

    os.makedirs(args.output_dir, exist_ok=True)

    # Prepare dataset
    loaded_npy = create_dataset_and_evalmetrix(args)

    print('Loading testset, phase test')
    testset = DatasetFLViT(args, loaded_npy, phase = 'test')
    test_loader = DataLoader(testset, sampler=SequentialSampler(testset), batch_size=args.batch_size, num_workers=args.num_workers)


    # if not celeba then get the union val dataset,
    if args.dataset not in ['celeba', 'gldk23', 'isic19']:
        print('Loading valset, phase val')
        valset = DatasetFLViT(args, loaded_npy, phase = 'val')
        val_loader = DataLoader(valset, sampler=SequentialSampler(valset), batch_size=args.batch_size, num_workers=args.num_workers)

    # Configuration for FedAVG, prepare model, optimizer, scheduler
    model_all, optimizer_all, scheduler_all = Partial_Client_Selection(args, model)

    #### Add server optimizer ####
    trainable_params_name, init_trainable_params = trainable_params(model, requires_name=True)
    global_params_dict: OrderedDict[str, torch.nn.Parameter] = OrderedDict(zip(trainable_params_name, deepcopy(init_trainable_params)))
    server_optimizer = server_optimization_fun(args, global_params_dict)

    # Train
    print("=============== Running training ===============")
    loss_fct = torch.nn.CrossEntropyLoss()
    tot_clients = args.dis_cvs_files
    epoch = -1


    while True:

        delta_cache = []
        weight_cache = []

        epoch += 1
        # randomly select partial clients
        if args.num_local_clients == len(args.dis_cvs_files):
            # just use all the local clients
            cur_selected_clients = args.proxy_clients
        else:
            cur_selected_clients = np.random.choice(tot_clients, args.num_local_clients, replace=False).tolist()

        # Get the quantity of clients joined in the FL train for updating the clients weights
        cur_tot_client_Lens = 0
        for client in cur_selected_clients:
            cur_tot_client_Lens += args.clients_with_len[client]

        val_loader_proxy_clients = {}

        for cur_single_client, proxy_single_client in zip(cur_selected_clients, args.proxy_clients):
            args.single_client = cur_single_client
            args.clients_weightes[proxy_single_client] = args.clients_with_len[cur_single_client] / cur_tot_client_Lens

            print('Loading trainset, phase train')
            trainset = DatasetFLViT(args, loaded_npy, phase='train')
            train_loader = DataLoader(trainset, sampler=RandomSampler(trainset), batch_size=args.batch_size, num_workers=args.num_workers)

            if args.dataset == 'celeba' or  args.dataset == 'gldk23' or args.dataset == 'isic19':
                valset = DatasetFLViT(args, loaded_npy, phase='val')
                val_loader_proxy_clients[proxy_single_client] = DataLoader(valset, sampler=SequentialSampler(valset), batch_size=args.batch_size,
                                          num_workers=args.num_workers)
            else:
                # for Cifar10 datasets we use union validation dataset
                val_loader_proxy_clients[proxy_single_client] = val_loader

            model = model_all[proxy_single_client]
            optimizer = optimizer_all[proxy_single_client]
            scheduler = scheduler_all[proxy_single_client]

            model = model.to(args.device).train()
            optimizer_to(optimizer, args.device)

            if args.decay_type == 'step':
                scheduler.step()

            print('Train the client', cur_single_client, 'of communication round', epoch)


            for inner_epoch in range(args.local_epochs):
                for step, batch in enumerate(train_loader):  
                    args.global_step_per_client[proxy_single_client] += 1
                    batch = tuple(t.to(args.device) for t in batch)

                    x, y = batch
                    predict = model(x)
                    loss = loss_fct(predict.view(-1, args.num_classes), y.view(-1))

                    loss.backward()

                    if args.grad_clip:
                        torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

                    optimizer.step()
                    optimizer.zero_grad()

                    if not args.decay_type == 'step':
                        scheduler.step()
                    args.learning_rate_record[proxy_single_client].append(optimizer.param_groups[0]['lr'])

                    if (step+1 ) % 10 == 0:
                        print(cur_single_client, step,':', len(train_loader),'inner epoch', inner_epoch, 'round', epoch,':',
                              args.max_communication_rounds, 'loss', loss.item(), 'lr', optimizer.param_groups[0]['lr'])

            # compute the deltas and weight_for each client 
            delta = OrderedDict()
            for (name, p0), p1 in zip(global_params_dict.items(), trainable_params(model)):
                delta[name] = p0.to(args.device) - p1

            delta_cache.append(delta)
            weight_cache.append(len(train_loader.dataset)) # dimention of the local dataset
                
            # we use frequent transfer of model between GPU and CPU due to limitation of GPU memory
            model.to('cpu')
            optimizer_to(optimizer, 'cpu')

        average_model(args, model_all, server_optimizer, global_params_dict, delta_cache, weight_cache)

        # then evaluate
        for cur_single_client, proxy_single_client in zip(cur_selected_clients, args.proxy_clients):
            args.single_client = cur_single_client
            model = model_all[proxy_single_client]
            model.to(args.device)
            valid(args, model, val_loader_proxy_clients[proxy_single_client], test_loader, TestFlag=True)
            model.cpu()

        args.record_val_acc = pd.concat([args.record_val_acc, pd.DataFrame([args.current_acc])], ignore_index=True)
        args.record_val_acc.to_csv(os.path.join(args.output_dir, 'val_acc.csv'))


        args.record_test_acc  = pd.concat([args.record_test_acc, pd.DataFrame([args.current_test_acc])], ignore_index=True)
        args.record_test_acc.to_csv(os.path.join(args.output_dir, 'test_acc.csv'))

        np.save(args.output_dir + '/learning_rate.npy', args.learning_rate_record)

        # save test acc
        tmp_round_acc = [val for val in args.current_test_acc.values() if type(val) != list]
        scalar_test_acc = np.asarray(tmp_round_acc).mean()
        # save al acc 
        tmp_round_val_acc =  [val for val in args.current_acc.values() if type(val) != list]
        scalar_val_acc = np.asarray(tmp_round_val_acc).mean()
        
        print("Epoch {}: Avg test acc {}, Avg Val acc {}".format(epoch, scalar_test_acc, scalar_val_acc))


        # log on wandb 
        if args.use_wandb: 
            metrics = {"train/avg_test_acc": scalar_test_acc, 'train/avg_val_acc': scalar_val_acc}
            wandb.log(metrics, step=epoch)

        if args.global_step_per_client[proxy_single_client] >= args.t_total[proxy_single_client]:
            break


    print("================End training! ================ ")

    if args.use_wandb:
        wandb.finish()


def main():
    parser = argparse.ArgumentParser()
    # General DL parameters
    parser.add_argument("--FL_platform", type = str, default="ViT-FedAVG",  help="Choose of different FL platform. ")
    parser.add_argument("--norm", type = str, default=None,  help="Selects a normalization layer for the model. Options: BN, LN, GN")
    parser.add_argument("--dataset", choices=["cifar10", "celeba", "pacs", "gldk23", "isic19"], default="cifar10", help="Which dataset.")
    parser.add_argument("--data_path", type=str, default='./data/', help="Where is dataset located.")

    parser.add_argument("--save_model_flag",  action='store_true', default=False,  help="Save the best model for each client.")
    parser.add_argument("--cfg",  type=str, default="configs/swin_tiny_patch4_window7_224.yaml", metavar="FILE", help='path to args file for Swin-FL',)

    parser.add_argument('--pretrained', type=bool, default=True, help="Whether use pretrained or not")
    parser.add_argument("--pretrained_dir", type=str, default="checkpoint/swin_tiny_patch4_window7_224.pth", help="Where to search for pretrained ViT models. [ViT-B_16.npz,  imagenet21k+imagenet2012_R50+ViT-B_16.npz]")
    parser.add_argument("--output_dir", default="output", type=str, help="The output directory where checkpoints/results/logs will be written.")
    parser.add_argument("--optimizer_type", default="sgd",choices=["sgd", "adamw"], type=str, help="Ways for optimization.")
    parser.add_argument("--num_workers", default=8, type=int, help="num_workers")
    parser.add_argument("--weight_decay", default=0, choices=[0.05, 0], type=float, help="Weight deay if we apply some. 0 for SGD and 0.05 for AdamW in paper")
    parser.add_argument('--grad_clip', action='store_true', default=True, help="whether gradient clip to 1 or not")

    parser.add_argument("--img_size", default=224, type=int, help="Final train resolution")
    parser.add_argument("--batch_size", default=32, type=int,  help="Local batch size for training.")
    parser.add_argument("--gpu_ids", type=str, default='0', help="gpu ids: e.g. 0  0,1,2")

    parser.add_argument('--seed', type=int, default=42, help="random seed for initialization")
    parser.add_argument('--n', type=int, default=0, help = "nth repetition of the same experiment")
    parser.add_argument('--use_wandb', action='store_true', default=False, help = "use wandb")

    ## section 2:  DL learning rate related
    parser.add_argument("--decay_type", choices=["cosine", "linear", "step"], default="cosine",  help="How to decay the learning rate.")
    parser.add_argument("--warmup_steps", default=100, type=int, help="Step of training to perform learning rate warmup for if set for cosine and linear deacy.")
    parser.add_argument("--step_size", default=30, type=int, help="Period of learning rate decay for step size learning rate decay")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,  help="Max gradient norm.")
    parser.add_argument("--learning_rate", default=3e-2, type=float,  help="The initial learning rate for SGD. Set to [3e-3] for ViT-CWT")

    ## section 3: fedopt params
    parser.add_argument("--server_optimizer_type", type = str, default="sgd", choices=["adam", "sgd"], help="Optimizer type for the Server")
    parser.add_argument("--server_learning_rate", default=1, type=float,  help="The initial learning rate for the Server optimizer.")
    parser.add_argument("--server_momentum", default=0.9, type=float,  help="Momentum for the Server optimizer")
    parser.add_argument("--server_weight_decay", default=0.0, type=float,  help="Weight Decay for the Server optimizer")


    ## FL related parameters
    parser.add_argument("--local_epochs", default=1, type=int, help="Local training epoch in FL")
    parser.add_argument("--max_communication_rounds", default=100, type=int,  help="Total communication rounds")
    parser.add_argument("--num_local_clients", default=-1, choices=[10, 20, -1], type=int, help="Num of local clients joined in each FL train. -1 indicates all clients")
    parser.add_argument("--split_type", type=str, choices=["split_1", "split_2", "split_3", "real", "central"], default="split_3", help="Which data partitions to use")


    args = parser.parse_args()

    # Initialization

    model = initization_configure(args)

    # Training, Validating, and Testing
    train(args, model)


    message = '\n \n ==============Start showing final performance ================= \n'
    message += 'Final union test accuracy is: %2.5f  \n' %  \
                   (np.asarray(list(args.current_test_acc.values())).mean())
    message += "================ End ================ \n"


    with open(args.file_name, 'a+') as args_file:
        args_file.write(message)
        args_file.write('\n')

    print(message)


if __name__ == "__main__":
    main()