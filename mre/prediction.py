import numpy as np
import torch
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, datasets, models
from collections import defaultdict
import torch.nn.functional as F
import time
import copy

# need data to be ordered thusly:
# image_sequence,width,hight,depth


class MREDataset(Dataset):
    def __init__(self, xa_ds, set_type='train', transform=None, clip=False):
        # inputs = ['T1Pre', 'T1Pos', 'T2SS', 'T2FR']
        inputs = ['T1Pre', 'T1Pos', 'T2SS']
        targets = ['elast']
        masks = ['liverMsk']

        # stack subject and z-slices to make 4 2D image groups for each 3D image group
        xa_ds = xa_ds.stack(subject_2d=('subject', 'z')).reset_index('subject_2d')
        subj_2d_coords = [f'{i.subject.values}_{i.z.values}' for i in xa_ds.subject_2d]
        xa_ds.assign_coords(subject_2d=subj_2d_coords)
        self.name_dict = dict(zip(range(len(subj_2d_coords)), subj_2d_coords))

        if set_type == 'train':
            input_set = xa_ds.subject_2d[20:]
        elif set_type == 'val':
            input_set = xa_ds.subject_2d[2:20]
        elif set_type == 'test':
            input_set = xa_ds.subject_2d[:2]
        else:
            raise AttributeError('Must choose one of ["train", "val", "test"] for `set_type`.')

        self.input_images = xa_ds.sel(sequence=inputs, subject_2d=input_set).transpose(
            'subject_2d', 'sequence', 'y', 'x').image.values
        # ds.sel(sequence=inputs, subject=subjects).stack(subject_2d=('subject','z')).transpose(
        # 'subject_2d','sequence', 'y', 'x').image.values.shape
        self.target_images = xa_ds.sel(sequence=targets, subject_2d=input_set).transpose(
            'subject_2d', 'sequence', 'y', 'x').image.values
        self.mask_images = xa_ds.sel(sequence=masks, subject_2d=input_set).transpose(
            'subject_2d', 'sequence', 'y', 'x').image.values
        self.transform = transform
        self.clip = clip

    def __len__(self):
        return len(self.input_images)

    def __getitem__(self, idx):
        image = self.input_images[idx]
        target = self.target_images[idx]
        if self.clip:
            image = np.where(image >= 750, 750, image)
            target = np.where(target >= 9000, 9000, target)
        mask = self.mask_images[idx]
        image = torch.Tensor(image)
        target = torch.Tensor(target)
        mask = torch.Tensor(mask)
        if self.transform:
            # image = self.transform(image)
            image = transforms.Normalize([image.mean()], [image.std()])(image)
            # target = self.transform(target)

        return [image, target, mask]


def masked_mse(pred, target, mask):
    pred = pred.contiguous()
    target = target.contiguous()
    mask = mask.contiguous()
    masked_mse = (((pred - target)*mask)**2).sum()/mask.sum()
    return masked_mse


def calc_loss(pred, target, mask, metrics, bce_weight=0.5):

    # bce = F.binary_cross_entropy_with_logits(pred, target)

    # pred = F.sigmoid(pred)
    # dice = dice_loss(pred, target)

    # loss = bce * bce_weight + dice * (1 - bce_weight)
    # loss = F.mse_loss(pred, target)
    loss = masked_mse(pred, target, mask)

    # metrics['bce'] += bce.data.cpu().numpy() * target.size(0)
    # metrics['dice'] += dice.data.cpu().numpy() * target.size(0)
    metrics['loss'] += loss.data.cpu().numpy() * target.size(0)

    return loss


def print_metrics(metrics, epoch_samples, phase):
    outputs = []
    for k in metrics.keys():
        outputs.append("{}: {:4f}".format(k, metrics[k] / epoch_samples))

    print("{}: {}".format(phase, ", ".join(outputs)))


def train_model(model, optimizer, scheduler, device, dataloaders, num_epochs=25, tb_writer=None):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    best_model_wts = copy.deepcopy(model.state_dict())
    best_loss = 1e16
    total_iter = 0
    for epoch in range(num_epochs):
        print('Epoch {}/{}'.format(epoch, num_epochs - 1))
        print('-' * 10)

        since = time.time()
        # Each epoch has a training and validation phase
        for phase in ['train', 'val']:
            if phase == 'train':
                scheduler.step()
                for param_group in optimizer.param_groups:
                    print("LR", param_group['lr'])

                model.train()  # Set model to training mode
            else:
                model.eval()   # Set model to evaluate mode
            metrics = defaultdict(float)
            epoch_samples = 0

            for inputs, labels, masks in dataloaders[phase]:
                inputs = inputs.to(device)
                labels = labels.to(device)
                masks = masks.to(device)
                # zero the parameter gradients
                optimizer.zero_grad()
                # forward
                # track history if only in train
                with torch.set_grad_enabled(phase == 'train'):
                    outputs = model(inputs)
                    loss = calc_loss(outputs, labels, masks, metrics)
                    if tb_writer:
                        tb_writer.add_scalar(f'loss_{phase}', loss, total_iter)
                    # backward + optimize only if in training phase
                    if phase == 'train':
                        loss.backward()
                        optimizer.step()
                # statistics
                epoch_samples += inputs.size(0)
                total_iter += 1
            print_metrics(metrics, epoch_samples, phase)
            epoch_loss = metrics['loss'] / epoch_samples
            # deep copy the model
            if phase == 'val' and epoch_loss < best_loss:
                print("saving best model")
                best_loss = epoch_loss
                best_model_wts = copy.deepcopy(model.state_dict())
        time_elapsed = time.time() - since
        print('{:.0f}m {:.0f}s'.format(time_elapsed // 60, time_elapsed % 60))
    print('Best val loss: {:4f}'.format(best_loss))
    # load best model weights
    model.load_state_dict(best_model_wts)
    return model
