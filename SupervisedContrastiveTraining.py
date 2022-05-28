import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import models
import numpy as np
from tqdm import tqdm


def hook(module, grad_in, grad_out):
    print('-' * 100)
    print(module)
    print(f'grad in {grad_in}, grad out {grad_out}')


class SupervisedContrast():
    def __init__(self, loader, num_classes=60):
        self.num_classes = num_classes
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        # student don't have momentum while teacher have
        self.student = models.wide_resnet50_2(num_classes=60).to(self.device)
        self.teacher = models.wide_resnet50_2(num_classes=60).to(self.device)
        self.momentum_synchronize(m=0)
        self.load_model()
        self.loader = loader

        self.student_mlp = nn.Sequential(
            nn.Linear(2048, 1024),
            nn.ReLU(),
            nn.Linear(1024, 512),
        ).to(self.device)

        self.teacher_mlp = nn.Sequential(
            nn.Linear(2048, 1024),
            nn.ReLU(),
            nn.Linear(1024, 512),
        ).to(self.device)

        self.initialize_queue()

    def student_forward(self, x):
        x = self.student.conv1(x)
        x = self.student.bn1(x)
        x = self.student.relu(x)
        x = self.student.maxpool(x)
        x = self.student.layer1(x)
        x = self.student.layer2(x)
        x = self.student.layer3(x)
        x = self.student.layer4(x)
        x = self.student.avgpool(x)
        x = x.squeeze(2).squeeze(2)
        return self.student_mlp(x)

    def teacher_forward(self, x):
        x = self.teacher.conv1(x)
        x = self.teacher.bn1(x)
        x = self.teacher.relu(x)
        x = self.teacher.maxpool(x)
        x = self.teacher.layer1(x)
        x = self.teacher.layer2(x)
        x = self.teacher.layer3(x)
        x = self.teacher.layer4(x)
        x = self.teacher.avgpool(x)
        x = x.squeeze(2).squeeze(2)
        return self.teacher_mlp(x)

    def load_model(self):
        if os.path.exists('teacher.pth'):
            self.teacher.load_state_dict(torch.load('teacher.pth', map_location=self.device))
            print('managed to load teacher')
            print('-' * 100)
        if os.path.exists('student.pth'):
            self.student.load_state_dict(torch.load('student.pth', map_location=self.device))
            print('managed to load student')
            print('-' * 100)

    def initialize_queue(self, queue_size=10):
        '''

        :param queue_size: total samples = queue_size*batch_size
        :return:
        '''
        self.queue = {}
        for i in range(self.num_classes):
            self.queue[i] = []

        self.enqueue(queue_size)
        print('managed to initialize queue!')
        print('-' * 100)

    def enqueue(self, size=1):
        '''

        :param size: queue_size*batch_size
        :return:
        '''
        with torch.no_grad():
            self.teacher.eval()
            for step, (x, y) in enumerate(self.loader):
                if step >= size:
                    break
                x = x.to(self.device)
                y = y.to(self.device)
                for i in range(self.num_classes):
                    mask = y == i
                    self.queue[i].append(self.teacher_forward(x[mask]))

    def dequeue(self, size=1):
        for i in range(self.num_classes):
            for _ in range(size):
                self.queue[i].pop(0)

    def momentum_synchronize(self, m=0.9):
        for s, t in zip(self.student.parameters(), self.teacher.parameters()):
            t.data = m * t.data + (1 - m) * s.data
            t.requires_grad = False

    def get_queue(self):
        y = []
        x = []
        for i in range(self.num_classes):
            now_x = torch.cat(self.queue[i], dim=0)
            x.append(now_x)
            y += [i] * now_x.shape[0]
        return torch.cat(x, dim=0), torch.tensor(y, device=self.device)

    def train(self, lr=1e-4, weight_decay=0, t=1, total_epoch=100):
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        criterion = self.chr_loss
        optimizer = torch.optim.AdamW(self.student.parameters(), lr=lr, weight_decay=weight_decay)
        print('now we start training!!!')
        for epoch in range(1, total_epoch + 1):
            self.student.train()
            train_loss = 0
            step = 0
            pbar = tqdm(self.loader)
            for x, y in pbar:
                x = x.to(device)
                y = y.to(device)
                x = self.student_forward(x)  # N, 60
                queue_x, queue_y = self.get_queue()
                loss = criterion(x, y, queue_x, queue_y, t=t)
                train_loss += loss.item()
                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_value_(self.student.parameters(), 0.1)
                optimizer.step()
                step += 1
                # scheduler.step()
                if step % 10 == 0:
                    pbar.set_postfix_str(f'loss = {train_loss / step}')
                self.enqueue()
                self.dequeue()
                self.momentum_synchronize()

            train_loss /= len(self.loader)
            print(f'epoch {epoch}, test loader loss = {train_loss}')
            self.save_model()

    def save_model(self):
        torch.save(self.student.state_dict(), 'student.pth')
        torch.save(self.teacher.state_dict(), 'teacher.pth')

    def chr_loss(self, x, y, queue_x, queue_y, t=1, ):
        '''
        :param x: N1, D
        :param y: N1,
        :param queue_x: N2,D
        :param queue_y: N2
        :param t:
        :return:
        '''
        x = F.normalize(x, dim=1)
        queue_x = F.normalize(queue_x, dim=1)
        gram = x @ queue_x.T / t  # N1, N2
        label_mask = y.float().unsqueeze(1) @ queue_y.float().unsqueeze(0)  # N1, N2
        log_probs = torch.log(F.softmax(gram, dim=1))
        loss = torch.sum(-label_mask * log_probs)/torch.sum(label_mask)
        return loss


if __name__ == '__main__':
    import argparse

    paser = argparse.ArgumentParser()
    paser.add_argument('-b', '--batch_size', default=128)
    paser.add_argument('-t', '--total_epoch', default=10)
    paser.add_argument('-l', '--lr', default=1e-4)
    args = paser.parse_args()
    batch_size = int(args.batch_size)
    total_epoch = int(args.total_epoch)
    lr = float(args.lr)

    train_image_path = './public_dg_0416/train/'
    valid_image_path = './public_dg_0416/train/'
    label2id_path = './dg_label_id_mapping.json'
    test_image_path = './public_dg_0416/public_test_flat/'
    from data.data import get_loader

    train_loader = get_loader(batch_size=batch_size,
                              valid_category=None,
                              train_image_path=train_image_path,
                              valid_image_path=valid_image_path,
                              label2id_path=label2id_path)
    a = SupervisedContrast(train_loader)
    a.train(total_epoch=total_epoch, lr=lr)