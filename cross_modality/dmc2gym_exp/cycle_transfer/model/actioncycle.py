

import os
import torch
from tqdm import tqdm
import torch.nn as nn
import numpy as np
from collections import OrderedDict
from torch.autograd import Variable
from torchvision import transforms
import matplotlib.pyplot as plt
import itertools


from model.dgmodel import img2state,state2img,imgDmodel,stateDmodel
from data.gymdata import Robotdata
from utils.utils import ImagePool,GANLoss
from model.fmodel import Fmodel,ImgFmodel,ADmodel,AGmodel


class ActionCycleGANModel():
    def __init__(self,opt):
        self.opt = opt
        self.isTrain = opt.istrain
        self.Tensor = torch.cuda.FloatTensor

        self.netG_A = state2img(opt=self.opt).cuda()
        self.netG_B = img2state(opt=self.opt).cuda()
        self.net_action_G_A = AGmodel(flag='A2B',opt=self.opt).cuda()
        self.net_action_G_B = AGmodel(flag='B2A',opt=self.opt).cuda()
        self.netF_A = Fmodel(self.opt).cuda()
        self.netF_B = ImgFmodel(opt=self.opt).cuda()
        self.dataF = Robotdata.get_loader(opt)
        self.train_forward_state(pretrained=opt.pretrain_f)
        #self.train_forward_img(pretrained=True)

        self.reset_buffer()


        # if self.isTrain:
        self.netD_A = imgDmodel(opt=self.opt).cuda()
        self.netD_B = stateDmodel(opt=self.opt).cuda()
        self.net_action_D_A = ADmodel(opt=self.opt).cuda()
        self.net_action_D_B = ADmodel(opt=self.opt).cuda()

        # if self.isTrain:
        self.fake_A_pool = ImagePool(pool_size=128)
        self.fake_B_pool = ImagePool(pool_size=128)
        self.fake_action_A_pool = ImagePool(pool_size=128)
        self.fake_action_B_pool = ImagePool(pool_size=128)
        # define loss functions
        self.criterionGAN = GANLoss(tensor=self.Tensor).cuda()
        if opt.loss == 'l1':
            self.criterionCycle = nn.L1Loss()
        elif opt.loss == 'l2':
            self.criterionCycle = nn.MSELoss()
        self.ImgcriterionCycle = nn.MSELoss()
        self.StatecriterionCycle = nn.L1Loss()
        # initialize optimizers
        parameters = [{'params':self.netF_A.parameters(),'lr':self.opt.F_lr},
                     {'params': self.netF_B.parameters(), 'lr': self.opt.F_lr},
                     {'params': self.netG_A.parameters(), 'lr': self.opt.G_lr},
                     {'params':self.netG_B.parameters(),'lr':self.opt.G_lr},
                     {'params': self.net_action_G_A.parameters(), 'lr': self.opt.G_lr},
                     {'params': self.net_action_G_B.parameters(), 'lr': self.opt.G_lr}]
        self.optimizer_G = torch.optim.Adam(parameters)
        self.optimizer_D_A = torch.optim.Adam(self.netD_A.parameters())
        self.optimizer_D_B = torch.optim.Adam(self.netD_B.parameters())
        self.optimizer_action_D_A = torch.optim.Adam(self.net_action_D_A.parameters())
        self.optimizer_action_D_B = torch.optim.Adam(self.net_action_D_B.parameters())

        print('---------- Networks initialized ---------------')
        print('-----------------------------------------------')


    def train_forward_state(self,pretrained=False):
        weight_path = './model/pred.pth'
        if pretrained:
            self.netF_A.load_state_dict(torch.load(weight_path))
            return None
        optimizer = torch.optim.Adam(self.netF_A.parameters(),lr=1e-3)
        loss_fn = nn.L1Loss()
        for epoch in range(50):
            epoch_loss = 0
            for i,item in enumerate(tqdm(self.dataF)):
                state, action, result = item[1]
                state = state.float().cuda()
                action = action.float().cuda()
                result = result.float().cuda()
                out = self.netF_A(state, action)
                loss = loss_fn(out, result)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                epoch_loss += loss.item()
            print('epoch:{} loss:{:.7f}'.format(epoch,epoch_loss/len(self.dataF)))
            torch.save(self.netF_A.state_dict(), weight_path)
        print('forward model has been trained!')

    def train_forward_img(self,pretrained=False):
        weight_path = './model/imgpred.pth'
        if pretrained:
            self.netF_B.load_state_dict(torch.load(weight_path))
            return None
        optimizer = torch.optim.Adam(self.netF_B.parameters(),lr=1e-3)
        loss_fn = nn.MSELoss()
        for epoch in range(50):
            epoch_loss = 0
            for i,item in enumerate(tqdm(self.dataF)):
                state, action, result = item[1]
                state = state.float().cuda()
                action = action.float().cuda()
                result = result.float().cuda()
                out = self.netF_B(state, action)*100
                loss = loss_fn(out, result)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                epoch_loss += loss.item()
            print('epoch:{} loss:{:.7f}'.format(epoch,epoch_loss/len(self.dataF)))
            torch.save(self.netF_B.state_dict(), weight_path)
        print('forward model has been trained!')

    def set_input(self, input):
        # A is state
        self.input_At0 = input[1][0]
        self.input_At1 = input[1][2]
        self.input_action_A = input[1][1]

        # B is img
        self.input_Bt0 = input[0][0]
        self.input_Bt1 = input[0][2]
        self.input_action_B = input[0][1]
        self.gt0 = input[2][0].float().cuda()
        self.gt1 = input[2][1].float().cuda()


    def forward(self):
        self.real_At0 = Variable(self.input_At0).float().cuda()
        self.real_At1 = Variable(self.input_At1).float().cuda()
        self.real_Bt0 = Variable(self.input_Bt0).float().cuda()
        self.real_Bt1 = Variable(self.input_Bt1).float().cuda()
        self.action_A = Variable(self.input_action_A).float().cuda()
        self.action_B = Variable(self.input_action_B).float().cuda()


    def test(self):
        # forward
        self.forward()
        # G_A and G_B
        self.backward_G()
        self.backward_D_B()


    def backward_D_basic(self, netD, real, fake):
        # Real
        pred_real = netD(real)
        loss_D_real = self.criterionGAN(pred_real, True)
        # Fake
        pred_fake = netD(fake.detach())
        loss_D_fake = self.criterionGAN(pred_fake, False)
        # Combined loss
        loss_D = (loss_D_real + loss_D_fake) * 0.5
        # backward
        if self.isTrain:
            loss_D.backward()
        return loss_D

    def backward_D_B(self):
        fake_A = self.fake_A_pool.query(self.fake_At0)
        loss_D_B = self.backward_D_basic(self.netD_B, self.real_At0, fake_A)
        self.loss_D_B = loss_D_B.item()

    def backward_D_A(self):
        fake_B = self.fake_B_pool.query(self.fake_Bt0)
        loss_D_A = self.backward_D_basic(self.netD_A, self.real_Bt0, fake_B)
        self.loss_D_A = loss_D_A.item()

    def backward_action_D_B(self):
        fake_action_A = self.fake_action_A_pool.query(self.fake_action_A)
        loss_action_D_B = self.backward_D_basic(self.net_action_D_B, self.action_A, fake_action_A)
        self.loss_action_D_B = loss_action_D_B.item()

    def backward_action_D_A(self):
        fake_action_B = self.fake_action_B_pool.query(self.fake_action_B)
        loss_action_D_A = self.backward_D_basic(self.net_action_D_A, self.action_B, fake_action_B)
        self.loss_action_D_A = loss_action_D_A.item()

    def backward_G(self):
        lambda_idt = 0.2
        lambda_C = self.opt.lambda_C
        lambda_G_B0 = 50.0
        lambda_G_B1 = 50.0
        lambda_G_action = 50.
        lambda_F = self.opt.lambda_F
        lambda_AC = self.opt.lambda_AC
        lambda_R = self.opt.lambda_R
        lambda_A_balance = 1.0

        # Identity loss
        if lambda_idt > 0:
            # G_A should be identity if real_B is fed.
            idt_A = self.net_action_G_A(self.action_B)
            loss_idt_A = self.criterionCycle(idt_A, self.action_B) * lambda_AC * lambda_idt
            # G_B should be identity if real_A is fed.
            idt_B = self.net_action_G_B(self.action_A)
            loss_idt_B = self.criterionCycle(idt_B, self.action_A) * lambda_AC * lambda_idt

            self.idt_A = idt_A.data
            self.idt_B = idt_B.data
            self.loss_idt_A = loss_idt_A.item()
            self.loss_idt_B = loss_idt_B.item()
        else:
            loss_idt_A = 0
            loss_idt_B = 0
            self.loss_idt_A = 0
            self.loss_idt_B = 0

        """
            GAN loss series
        """

        # GAN loss D_B(G_B(B)) for action
        fake_action_A = self.net_action_G_B(self.action_B)
        pred_fake = self.net_action_D_B(fake_action_A)
        loss_action_G_B = self.criterionGAN(pred_fake, True) * lambda_G_action

        # GAN loss D_A(G_A(A)) for action
        fake_action_B = self.net_action_G_A(self.action_A)
        pred_fake = self.net_action_D_A(fake_action_B)
        loss_action_G_A = self.criterionGAN(pred_fake, True) * lambda_G_action

        loss_gan_original = loss_action_G_B + loss_action_G_A + self.loss_idt_A + self.loss_idt_B


        """
            Cycle loss series
        """

        # Backward cycle loss for action_A
        rec_action_B = self.net_action_G_A(fake_action_A)
        loss_cycle_action_B = self.criterionCycle(rec_action_B, self.action_B) * lambda_AC

        # Backward cycle loss for action_B
        rec_action_A = self.net_action_G_B(fake_action_B)
        loss_cycle_action_A = self.criterionCycle(rec_action_A, self.action_A) * lambda_AC

        loss_cycle_original = loss_cycle_action_B + loss_cycle_action_A

        # combined loss
        loss_G = loss_gan_original + loss_cycle_original


        if self.isTrain:
            loss_G.backward()

        self.fake_At0 = self.gt0.data
        self.fake_At1 = self.gt1.data
        self.fake_Bt0 = self.gt0.data
        self.fake_Bt1 = self.gt1.data
        self.fake_action_A = fake_action_A.data
        self.fake_action_B = fake_action_B.data

        self.loss_G_action_B = loss_action_G_B.item()
        self.loss_G_action_A = loss_action_G_A.item()
        self.loss_cycle_action_A = loss_cycle_action_A.item()
        self.loss_cycle_action_B = loss_cycle_action_B.item()

        self.loss_state_lt0 = self.criterionCycle(self.fake_At0, self.gt0).item()
        self.loss_state_lt1 = self.criterionCycle(self.fake_At1, self.gt1).item()
        self.gt_buffer.append(self.gt0.cpu().data.numpy())
        self.gt_buffer.append(self.gt1.cpu().data.numpy())
        self.pred_buffer.append(self.fake_At0.cpu().data.numpy())
        self.pred_buffer.append(self.fake_At1.cpu().data.numpy())
        self.realA_buffer.append(self.action_A.cpu().data.numpy())
        self.fakeA_buffer.append(self.fake_action_B.cpu().data.numpy())
        self.realB_buffer.append(self.action_B.cpu().data.numpy())
        self.fakeB_buffer.append(self.fake_action_A.cpu().data.numpy())


    def optimize_parameters(self):
        # forward
        self.forward()
        # G_A and G_B
        self.optimizer_G.zero_grad()
        self.backward_G()
        self.optimizer_G.step()
        # action_D_B
        self.optimizer_action_D_B.zero_grad()
        self.backward_action_D_B()
        self.optimizer_action_D_B.step()
        # action_D_A
        self.optimizer_action_D_A.zero_grad()
        self.backward_action_D_A()
        self.optimizer_action_D_A.step()

    def get_current_errors(self):
        ret_errors = OrderedDict([('L_t0',self.loss_state_lt0), ('L_t1',self.loss_state_lt1),
                                  ('D_action_B', self.loss_action_D_B), ('D_action_A', self.loss_action_D_A),
                                  ('G_action_B', self.loss_G_action_B), ('G_action_A', self.loss_G_action_A),
                                  ('Cyc_action_B', self.loss_cycle_action_B), ('Cyc_action_A', self.loss_cycle_action_A)])
        return ret_errors

    # helper saving function that can be used by subclasses
    def save_network(self, network, network_label, path):
        save_filename = 'model_{}.pth'.format(network_label)
        save_path = os.path.join(path, save_filename)
        torch.save(network.state_dict(), save_path)

    def save(self, path):
        self.save_network(self.net_action_G_B, 'action_G_B', path)
        self.save_network(self.net_action_D_B, 'action_D_B', path)
        self.save_network(self.net_action_G_A, 'action_G_A', path)
        self.save_network(self.net_action_D_A, 'action_D_A', path)

    def load_network(self, network, network_label, path):
        weight_filename = 'model_{}.pth'.format(network_label)
        weight_path = os.path.join(path, weight_filename)
        network.load_state_dict(torch.load(weight_path))

    def load(self,path):
        self.load_network(self.netG_B, 'G_B', path)
        self.load_network(self.netD_B, 'D_B', path)
        self.load_network(self.netG_A, 'G_A', path)
        self.load_network(self.netD_A, 'D_A', path)

        self.load_network(self.net_action_G_B, 'action_G_B', path)
        self.load_network(self.net_action_D_B, 'action_D_B', path)
        self.load_network(self.net_action_G_A, 'action_G_A', path)
        self.load_network(self.net_action_D_A, 'action_D_A', path)

    def show_points(self):
        # num_images = min(imgs.shape[0],num_images)
        ncols = 2
        nrows = 4
        _, axes = plt.subplots(ncols, nrows, figsize=(nrows * 3, ncols * 3))
        axes = axes.flatten()
        gt_data = np.vstack(self.gt_buffer)
        pred_data = np.vstack(self.pred_buffer)
        print(abs(gt_data-pred_data).mean(0))

        realA = np.vstack(self.realA_buffer)
        fakeA = np.vstack(self.fakeA_buffer)
        realB = np.vstack(self.realB_buffer)
        fakeB = np.vstack(self.fakeB_buffer)

        for ax_i, ax in enumerate(axes):
            if ax_i < nrows:
                ax.scatter(realA[:,ax_i], fakeA[:, ax_i],s=3,label='action A')
            else:
                ax.scatter(realB[:,ax_i-nrows], fakeB[:, ax_i-nrows],s=3,label='action B')


    def npdata(self,item):
        return item.cpu().data.numpy()

    def reset_buffer(self):
        self.gt_buffer = []
        self.pred_buffer = []
        self.realA_buffer = []
        self.fakeA_buffer = []
        self.realB_buffer = []
        self.fakeB_buffer = []


    def visual(self,path):
        # plt.xlim(-4,4)
        # plt.ylim(-1.5,1.5)
        self.show_points()
        plt.legend()
        plt.savefig(path)
        plt.cla()
        plt.clf()
        self.reset_buffer()


if __name__ == '__main__':
    mymodel = CycleGANModel()
    print(mymodel)