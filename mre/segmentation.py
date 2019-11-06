import numpy as np
import torch
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, datasets, models
from collections import defaultdict
import torch.nn.functional as F
import torchvision.transforms.functional as TF
from torch.utils.data.sampler import RandomSampler
import time
import copy
from robust_loss_pytorch import adaptive
import warnings
from datetime import datetime
from tqdm import tqdm_notebook
from tensorboardX import SummaryWriter
import PIL


# need data to be ordered thusly:
# image_sequence,width,hight,depth


class ChaosDataset(Dataset):
    def __init__(self, xr_ds, set_type='train', transform=None, clip=False, seed=100,
                 test_subj='01', aug=True, sequence_mode='all', resize=False,
                 val_split=0.2, model_arch='3D', verbose=False):

        self.verbose = verbose
        self.model_arch = model_arch
        self.all_sequences = xr_ds.sequence.values
        self.my_sequence = sequence_mode
        if type(test_subj) is not list:
            test_subj = [test_subj]
        self.test_subj = test_subj
        xr_ds_test = xr_ds.sel(subject=self.test_subj)
        xr_ds = xr_ds.drop(self.test_subj, dim='subject')

        # default method of training/val split is random shuffling of the main list
        np.random.seed(seed)
        shuffle_list = np.asarray(xr_ds.subject)
        np.random.shuffle(shuffle_list)

        if set_type == 'test':
            input_set = self.test_subj
        elif set_type == 'val':
            # input_set = xr_ds.subject_2d[2:20]
            input_set = list(shuffle_list[0:3])
        elif set_type == 'train':
            # input_set = xr_ds.subject_2d[:2]
            input_set = list(shuffle_list[3:])
        else:
            raise AttributeError('Must choose one of ["train", "val", "test"] for `set_type`.')
        self.my_subjects = input_set

        # pick correct input set, remove test subjects
        if set_type == 'test':
            xr_ds = xr_ds_test
        else:
            xr_ds = xr_ds.sel(subject=input_set)

        # assign input and target elements, based on 2d or 3d arch
        if self.model_arch == '2D':
            xr_ds = xr_ds.stack(subject_2d=('subject', 'z')).reset_index('subject_2d')
            subj_2d_coords = [f'{i.subject.values}_{i.z.values}' for i in xr_ds.subject_2d]
            xr_ds = xr_ds.assign_coords(subject_2d=subj_2d_coords)
            bad_slices = []
            for i in subj_2d_coords:
                if xr_ds.sel(subject_2d=i).mask.sum() < 10:
                    bad_slices.append(i)
            xr_ds = xr_ds.drop(bad_slices, dim='subject_2d')

            self.names = xr_ds.subject_2d.values
            self.input_images = xr_ds.sel(sequence=self.my_sequence)['image'].transpose(
                'subject_2d', 'sequence', 'y', 'x').values
            self.input_images = self.input_images.astype(np.float32)
            self.target_images = xr_ds.sel(sequence=self.my_sequence)['mask'].transpose(
                'subject_2d', 'sequence', 'y', 'x').values
            self.target_images = self.target_images.astype(np.int32)

        else:
            input_images = xr_ds['image'].transpose(
                'subject', 'sequence', 'z', 'y', 'x')
            target_images = xr_ds['mask'].transpose(
                'subject', 'sequence', 'z', 'y', 'x')

            if self.my_sequence != 'all':
                self.input_images = input_images.sel(sequence=self.my_sequence).values
                self.target_images = target_images.sel(sequence=self.my_sequence).values
            else:
                # Stack all the sequences together for appropriate random sampling
                if self.verbose:
                    print('stacking sequences input xarray')
                n_subj = len(input_images.subject)
                n_seq = len(input_images.sequence)
                z = len(input_images.z)
                y = len(input_images.y)
                x = len(input_images.x)
                self.input_images = input_images.values.reshape(n_subj*n_seq, 1, z, y, x)
                if self.verbose:
                    print('stacking sequences target xarray')
                n_subj = len(target_images.subject)
                n_seq = len(target_images.sequence)
                z = len(target_images.z)
                y = len(target_images.y)
                x = len(target_images.x)
                self.target_images = target_images.values.reshape(n_subj*n_seq, 1, z, y, x)

            self.input_images = self.input_images.astype(np.float32)
            self.target_images = self.target_images.astype(np.int32)
            self.names = np.concatenate([xr_ds.subject.values for seq in self.all_sequences])

        # Additional flags
        self.transform = transform
        self.aug = aug
        self.clip = clip
        self.resize = resize

    def __len__(self):
        return len(self.input_images)

    def __getitem__(self, idx):

        if self.verbose:
            print(self.names[idx], f'{idx}/{self.__len__()}')
        if self.model_arch == '3D':
            image, target = self.get_data_aug_3d(idx)
        elif self.model_arch == '2D':
            image, target = self.get_data_aug_2d(idx)

        image = torch.Tensor(image)
        target = torch.Tensor(target)

        return [image, target, self.names[idx]]

    def get_data_aug_3d(self, idx):
        '''get data (image and target), and apply augmentations if indicated'''
        image = self.input_images[idx]
        target = self.target_images[idx]

        if self.clip:
            # t1 image clip
            if (('t1' in self.my_sequence) or
                    (self.my_sequence == 'all' and idx < 2*len(self.my_subjects))):
                image = np.where(image >= 1500, 1500, image)
            # t2 image clip
            else:
                image = np.where(image >= 2000, 2000, image)
            target = np.where(target > 0, 1, 0).astype(np.int32)

        if self.transform:
            if self.aug:
                rot_angle = np.random.uniform(-2, 2, 1)
                translations = np.random.uniform(-2, 2, 2)
                scale = np.random.uniform(0.95, 1.05, 1)
                restack = 0
                flip = 0
            else:
                rot_angle = 0
                translations = (0, 0)
                scale = 1
                restack = 0
                flip = 0
            image = self.input_transform_3d(image, rot_angle,
                                            translations, scale, restack, flip,
                                            resample=PIL.Image.BILINEAR)
            target = self.affine_transform_3d(target, rot_angle,
                                              translations, scale, restack, flip,
                                              resample=PIL.Image.NEAREST)
        return image, target

    def get_data_aug_2d(self, idx):
        '''get data (image and target), and apply augmentations if indicated'''
        image = self.input_images[idx]
        target = self.target_images[idx]

        if self.clip:
            for i, seq in enumerate(self.my_sequence):
                if 't1' in seq:
                    image[i, :] = np.where(image[i, :] >= 1500, 1500, image[i, :])
                else:
                    image[i, :] = np.where(image[i, :] >= 2000, 2000, image[i, :])
            target = np.where(target > 0, 1, 0).astype(np.int32)

        if self.transform:
            if self.aug:
                rot_angle = np.random.uniform(-5, 5, 1)
                translations = np.random.uniform(-5, 5, 2)
                scale = np.random.uniform(0.90, 1.00, 1)
                flip = np.random.randint(0, 2)
            else:
                rot_angle = 0
                translations = (0, 0)
                scale = 1
                flip = 0
            for i in range(len(self.my_sequence)):
                image[i, :] = self.input_transform_2d(image[i, :], rot_angle,
                                                      translations, scale, flip)

                target[i, :] = self.affine_transform_2d(target[i, :], rot_angle,
                                                        translations, scale, flip)
        return image, target

    def input_transform_3d(self, input_image, rot_angle=0, translations=0, scale=1, restack=0,
                           flip=0, resample=None):
        # normalize and offset image
        image = input_image
        # image = np.where(input_image <= 1e-9, np.nan, input_image)
        mean = np.nanmean(image)
        std = np.nanstd(image)
        # image = ((image - mean)/std) + 4
        image = ((image - mean)/std)
        image = np.where(image != image, 0, image)

        # perform affine transfomrations
        image = self.affine_transform_3d(image, rot_angle, translations, scale, restack, flip,
                                         resample=resample)
        return image

    def input_transform_2d(self, input_image, rot_angle=0, translations=0, scale=1,
                           flip=0, resample=None):
        # normalize and offset image
        image = input_image
        # image = np.where(input_image <= 1e-9, np.nan, input_image)
        mean = np.nanmean(image)
        std = np.nanstd(image)
        # image = ((image - mean)/std) + 4
        image = ((image - mean)/std)
        image = np.where(image != image, 0, image)

        # perform affine transfomrations
        image = self.affine_transform_2d(image, rot_angle, translations, scale, flip,
                                         resample=resample)
        return image

    def affine_transform_3d(self, image, rot_angle=0, translations=0, scale=1, restack=0, flip=0,
                            resample=None):
        output_image = image.copy()
        if self.verbose:
            print(f'rot_angle: {rot_angle}, translations: {translations},'
                  f'scale: {scale}, restack={restack}')

        # for i in range(output_image.shape[0]):
        #     if (i+restack < 0) or (i+restack > output_image.shape[0]):
        #         output_image[0, i] = np.zeros_like(output_image[0, 0])
        #     else:
        #         output_image[0, i] = self.affine_transform_slice(output_image[0, i+restack],
        #                                                          rot_angle, translations, scale)

        for i in range(output_image.shape[1]):
            if (i+restack < 0) or (i+restack > output_image.shape[1]-1):
                output_image[0, i] = np.zeros_like(output_image[0, 0])
            else:
                output_image[0, i] = self.affine_transform_2d(image[0, i+restack],
                                                              rot_angle, translations, scale, flip,
                                                              resample=resample)
        return output_image

    def affine_transform_2d(self, input_slice, rot_angle=0, translations=0, scale=1, flip=0,
                            resample=None):
        input_slice = transforms.ToPILImage()(input_slice)
        input_slice = TF.affine(input_slice, angle=rot_angle,
                                translate=list(translations), scale=scale, shear=0,
                                resample=resample)
        if flip:
            input_slice = TF.hflip(input_slice)
        input_slice = transforms.ToTensor()(input_slice)
        return input_slice
