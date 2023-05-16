import argparse
from loader import BioDataset_aug
from torch_geometric.data import DataLoader
from torch_geometric.nn.inits import uniform
from torch_geometric.nn import global_mean_pool
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from tqdm import tqdm
import numpy as np

from model import GNN
from sklearn.metrics import roc_auc_score

import pandas as pd
from torch.autograd import Variable
from copy import deepcopy


def cycle_index(num, shift):
    arr = torch.arange(num) + shift
    arr[-shift:] = torch.arange(shift)
    return arr

class Discriminator(nn.Module):
    def __init__(self, hidden_dim):
        super(Discriminator, self).__init__()
        self.weight = nn.Parameter(torch.Tensor(hidden_dim, hidden_dim))
        self.reset_parameters()

    def reset_parameters(self):
        size = self.weight.size(0)
        uniform(size, self.weight)

    def forward(self, x, summary):
        h = torch.matmul(summary, self.weight)
        return torch.sum(x*h, dim = 1)

class graphcl(nn.Module):
    def __init__(self, gnn):
        super(graphcl, self).__init__()
        self.gnn = gnn
        self.pool = global_mean_pool
        self.projection_head = nn.Sequential(nn.Linear(300, 300), nn.ReLU(inplace=True), nn.Linear(300, 300))

    def forward_cl(self, x, edge_index, edge_attr, batch):
        x = self.gnn(x, edge_index, edge_attr)
        x = self.pool(x, batch)
        x = self.projection_head(x)
        return x

    def loss_cl(self, x1, x2):
        T = 0.1
        batch_size, _ = x1.size()
        x1_abs = x1.norm(dim=1)
        x2_abs = x2.norm(dim=1)

        sim_matrix = torch.einsum('ik,jk->ij', x1, x2) / torch.einsum('i,j->ij', x1_abs, x2_abs)
        sim_matrix = torch.exp(sim_matrix / T)
        pos_sim = sim_matrix[range(batch_size), range(batch_size)]
        loss = pos_sim / (sim_matrix.sum(dim=1) - pos_sim)
        loss = - torch.log(loss).mean()
        return loss

def gen_ran_output_old(data, model, args, device):
    vice_model = deepcopy(model)
    for (name,vice_model), (name,param) in zip(vice_model.named_parameters(), model.named_parameters()):
        if name.split('.')[0] == 'projection_head':
            vice_model.data = param.data
        else:
            vice_model.data = param.data + args.eta * torch.normal(0,torch.ones_like(param.data)*param.data.std()).to(device)  
    z2 = vice_model.forward_cl(data.x, data.edge_index, data.edge_attr, data.batch)
    #z2 = vice_model.forward(data.x, data.edge_index, data.edge_attr, data.batch)
    return z2

def gen_ran_output(data, model, args):
    vice_model = deepcopy(model)
    for (adv_name,adv_param), (name,param) in zip(vice_model.named_parameters(), model.named_parameters()):
        if name.split('.')[0] == 'proj_head':
            adv_param.data = param.data
        else:
            adv_param.data = param.data + args.eta * torch.normal(0,torch.ones_like(param.data)*param.data.std()).to(args.device)
    z2 = vice_model.forward_cl(data.x, data.edge_index, data.edge_attr, data.batch)
    #z2 = vice_model(data.x, data.edge_index, data.batch, data.num_graphs)
    return z2

def get_grad(embed, loss):
    grads = torch.autograd.grad(loss,
                                        embed,
                                        create_graph=True,
                                        only_inputs=True)
    grad = grads[0]
    return grad

def train(args, model, device, dataset, optimizer):

    dataset.aug = "none"
    dataset = dataset.shuffle()
    loader1 = DataLoader(dataset, batch_size=args.batch_size, num_workers = args.num_workers, shuffle=False)
    model.train()

    train_acc_accum = 0
    train_loss_accum = 0

    for step, data in enumerate(tqdm(loader1, desc="Iteration")):
        data = data.to(device)
        optimizer.zero_grad()
        x2 = gen_ran_output(data, model, args)
        x1 = model.forward_cl(data.x, data.edge_index, data.edge_attr, data.batch)
        #x2 = gen_ran_output(data, model, args, device)
        x2 = Variable(x2.detach().data, requires_grad=True)
        lossf = model.loss_cl(x1, x2)
        #lossf = model.loss_cal(x2, x1, args)
        a = args.a
        if a == 0.0:
            loss = lossf
        else:
            grads1, grads2 = get_grad(x1, lossf), get_grad(x2, lossf)
            lossg = model.loss_cl(grads1, grads2)
            # a = 0.4
            loss = (1 - a) * lossf + a * lossg
        #print(loss)
        loss.backward()
        optimizer.step()

        train_loss_accum += float(loss.detach().cpu().item())
        acc = torch.tensor(0)
        train_acc_accum += float(acc.detach().cpu().item())

    return train_acc_accum/(step+1), train_loss_accum/(step+1)


def main():
    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch implementation of pre-training of graph neural networks')
    parser.add_argument('--device', type=int, default=0,
                        help='which gpu to use if any (default: 0)')
    parser.add_argument('--batch_size', type=int, default=256,
                        help='input batch size for training (default: 256)')
    parser.add_argument('--epochs', type=int, default=100,
                        help='number of epochs to train (default: 100)')
    parser.add_argument('--lr', type=float, default=0.001,
                        help='learning rate (default: 0.001)')
    parser.add_argument('--decay', type=float, default=0,
                        help='weight decay (default: 0)')
    parser.add_argument('--num_layer', type=int, default=5,
                        help='number of GNN message passing layers (default: 5).')
    parser.add_argument('--emb_dim', type=int, default=300,
                        help='embedding dimensions (default: 300)')
    parser.add_argument('--dropout_ratio', type=float, default=0,
                        help='dropout ratio (default: 0)')
    parser.add_argument('--JK', type=str, default="last",
                        help='how the node features across layers are combined. last, sum, max or concat')
    parser.add_argument('--gnn_type', type=str, default="gin")
    parser.add_argument('--model_file', type = str, default = '', help='filename to output the pre-trained model')
    parser.add_argument('--seed', type=int, default=0, help = "Seed for splitting dataset.")
    parser.add_argument('--num_workers', type=int, default = 4, help='number of workers for dataset loading')
    # Random
    parser.add_argument('--eta', type=float, default=0.1)
    #grad_weight
    parser.add_argument('--a', type=float, default=0.4)
    args = parser.parse_args()

    torch.manual_seed(0)
    np.random.seed(0)
    device = torch.device("cuda:" + str(args.device)) if torch.cuda.is_available() else torch.device("cpu")
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(0)

    #set up dataset
    root_unsupervised = 'dataset/unsupervised'
    dataset = BioDataset_aug(root_unsupervised, data_type='unsupervised')
    # dataset.data.to(device)
    print(dataset)
    #set up model
    gnn = GNN(args.num_layer, args.emb_dim, JK = args.JK, drop_ratio = args.dropout_ratio, gnn_type = args.gnn_type).to(device)

    model = graphcl(gnn)
    #model = graphcl(gnn)
    model.to(device)
    #set up optimizer
    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.decay)
    print(optimizer)

    for epoch in range(1, args.epochs):
        print("====epoch " + str(epoch))
        train_acc, train_loss = train(args, model, device, dataset, optimizer)
        print(train_acc)
        print(train_loss) 
        if epoch % 20 == 0:
            torch.save(gnn.state_dict(), "./models_simgrace/simgrace_" + str(args.a) + '_' + str(epoch) + ".pth")
if __name__ == "__main__":
    main()
