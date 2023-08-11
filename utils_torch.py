import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from scipy.io import loadmat, savemat
from sklearn.model_selection import train_test_split

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from torchvision.models import VisionTransformer
from torchmetrics.image import StructuralSimilarityIndexMeasure as torch_SSIM

# Define custom dataset classes for train and test sets
class CustomDataset(Dataset):
    def __init__(self, filenames):
        self.root_X,      self.root_y      = 'simulations2D/input_features', 'simulations2D/output_targets'
        self.filenames_X, self.filenames_y = filenames

    def __len__(self):
        return len(self.filenames_X)

    def __getitem__(self, idx):
        X_path = os.path.join(self.root_X, self.filenames_X[idx])
        y_path = os.path.join(self.root_y, self.filenames_y[idx])
        X_array, y_array = np.load(X_path), np.load(y_path)
        
        X_normalized = (X_array - X_array.min()) / (X_array.max() - X_array.min())
        y_normalized = (y_array - y_array.min()) / (y_array.max() - y_array.min())
        X_tensor = torch.from_numpy(X_normalized)
        y_tensor = torch.from_numpy(y_normalized)
        return X_tensor, y_tensor
    
# Define your root folders
X_fname, y_fname = os.listdir('simulations2D/input_features'), os.listdir('simulations2D/output_targets')
X_train_fname, X_test_fname, y_train_fname, y_test_fname = train_test_split(X_fname, y_fname, test_size=0.25, random_state=42)

# Create custom dataset instances for train and test sets
train_dataset = CustomDataset([X_train_fname, y_train_fname])
test_dataset  = CustomDataset([X_test_fname,  y_test_fname])

# Create dataloaders for train and test sets
batch_size = 32
train_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
test_dataloader  = DataLoader(test_dataset,  batch_size=batch_size, shuffle=False)

### Separable Convolutions ###
class SeparableConv2d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=(3,3), bias=False):
        super(SeparableConv2d, self).__init__()
        self.depthwise = nn.Conv2d(in_channels, in_channels,  kernel_size=kernel_size, groups=in_channels, bias=bias, padding=1)
        self.pointwise = nn.Conv2d(in_channels, out_channels, kernel_size=1, bias=bias)
    
    def forward(self, x):
        out = self.depthwise(x)
        out = self.pointwise(out)
        return out

### Time-Distributed Layer ###
class TimeDistributedModule(nn.Module):
    def __init__(self, module):
        super(TimeDistributedModule, self).__init__()
        self.module = module
    
    def forward(self, x):
        batch_size, time_steps, channels, height, width = x.size()
        x = x.reshape(-1, channels, height, width)
        module_output = self.module(x)
        module_output = module_output.reshape(batch_size, time_steps, -1, module_output.size(2), module_output.size(3))
        return module_output


### Convolutional (Encoder) block ###
class conv_block(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(conv_block, self).__init__()
        self.conv1 = SeparableConv2d(in_channels, in_channels)
        self.conv2 = SeparableConv2d(in_channels, out_channels)
        self.norm = nn.InstanceNorm2d(out_channels)
        self.actv = nn.GELU()
        self.pool = nn.AvgPool2d(kernel_size=2, stride=2)

    def forward(self, x):
        x = self.conv2(self.conv1(x))
        x = self.norm(x)
        x = self.actv(x)
        x = self.pool(x)
        return x

### Transpose Convolutional (Decoder) block ###
class decon_block(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(decon_block, self).__init__()
        self.deconv = nn.ConvTranspose2d(in_channels, out_channels, kernel_size=3, stride=2, padding=1, output_padding=1)
        self.norm = nn.InstanceNorm2d(out_channels)
        self.actv = nn.GELU()

    def forward(self, x):
        x = self.deconv(x)
        x = self.norm(x)
        x = self.actv(x)
        return x

### Vision Transformer ####
class VisionTransformer(nn.Module):
    def __init__(self, input_channels, output_channels):
        super(VisionTransformer, self).__init__()
        self.projector = nn.Sequential(
            nn.Conv2d(input_channels, output_channels, kernel_size=1),
            nn.GELU())
        self.attention = nn.MultiheadAttention(embed_dim=output_channels, num_heads=16)
        self.ffn = nn.Sequential(
            nn.Linear(output_channels, output_channels),
            nn.GELU(),
            nn.Linear(output_channels, output_channels))

    def forward(self, x):
        projected_x = self.projector(x)
        batch_size, channels, height, width = projected_x.size()
        x_reshaped = projected_x.view(batch_size, channels, -1).permute(2, 0, 1)
        attended_x, _ = self.attention(x_reshaped, x_reshaped, x_reshaped)
        attended_x = attended_x.permute(1, 2, 0).view(batch_size, channels, height, width)
        attended_x = attended_x.permute(0, 2, 1, 3).contiguous().view(batch_size, height * width, channels)
        ffn_output = self.ffn(attended_x)
        ffn_output = ffn_output.view(batch_size, height, width, channels).permute(0, 3, 1, 2)
        return ffn_output

### Recurrent Block ###
class RecurrentBlock(nn.Module):
    def __init__(self, input_dim, hidden_dim, timesteps=60, im_size=16):
        super(RecurrentBlock, self).__init__()
        self.timesteps = timesteps
        self.im_size = im_size
        self.input_dim, self.hidden_dim = input_dim, hidden_dim
        self.lstm = nn.LSTM(batch_first = True,
                            input_size  = self.input_dim * self.im_size * self.im_size, 
                            hidden_size = self.hidden_dim * self.im_size * self.im_size)
        
    def forward(self, x):
        batch_size, channels, height, width = x.size()
        x_reshaped = x.reshape(batch_size, channels*height*width)
        x_repeated = x_reshaped.view(batch_size, 1, channels*height*width).repeat(1, self.timesteps, 1)
        x_recurrent, _ = self.lstm(x_repeated)
        
        # Reshape the LSTM output to match the dimensions
        x_output = x_recurrent.view(batch_size, self.timesteps, self.hidden_dim, self.im_size, self.im_size)
        return x_output
    
class ProxyModel(nn.Module):
    def __init__(self):
        super(ProxyModel, self).__init__()

        self.encoder = nn.Sequential(
            conv_block(4, 8),
            conv_block(8, 16),
            conv_block(16, 32))

        self.vit = VisionTransformer(32, 64)
        
        self.recurrent = RecurrentBlock(64, 32, im_size=2**3)
        
        self.decoder = nn.Sequential(
            TimeDistributedModule(decon_block(32, 16)),
            TimeDistributedModule(decon_block(16, 8)),
            TimeDistributedModule(decon_block(8, 2)))

    def forward(self, x):
        encoded     = self.encoder(x)
        vit_encoded = self.vit(encoded)
        rnn_encoded = self.recurrent(vit_encoded)
        decoded     = self.decoder(rnn_encoded)
        return decoded

class CustomLoss(nn.Module):
    def __init__(self, mse_weight=0.5, ssim_weight=0.5):
        super(CustomLoss, self).__init__()
        self.mse_weight = mse_weight
        self.ssim_weight = ssim_weight
        self.ssim = torch_SSIM(data_range=1.0)

    def forward(self, output, target):
        # mse loss
        mse_loss = F.mse_loss(output, target)
        # ssim loss
        ssim_losses = []
        for t in range(output.size(1)):
                pred_single = output[:, t]
                target_single = target[:, t]
                ssim_loss = 1 - self.ssim(pred_single, target_single)
                ssim_losses.append(ssim_loss)
        ssim_loss = torch.stack(ssim_losses).mean()
        # total loss
        loss = self.mse_weight * mse_loss + self.ssim_weight * ssim_loss
        return loss
    
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = ProxyModel()
model.to(device)

criterion  = CustomLoss().to(device)
optimizer  = torch.optim.NAdam(model.parameters(), lr=0.001)
num_epochs = 20

for epoch in range(num_epochs):
    model.train()
    train_loss = 0.0

    train_subset_size = int(len(train_dataset) * 0.8)  # 80% for training
    train_subset, val_subset = torch.utils.data.random_split(train_dataset, [train_subset_size, len(train_dataset) - train_subset_size])
    train_dataloader = DataLoader(train_subset, batch_size=batch_size, shuffle=True)
    valid_dataloader = DataLoader(val_subset, batch_size=batch_size, shuffle=False)

    for batch_idx, (x, y) in enumerate(train_dataloader):
        x, y = x.float().to(device), y.float().to(device)
        optimizer.zero_grad()
        y_pred = model(x)
        loss = criterion(y_pred, y)
        loss.backward()
        optimizer.step()
        train_loss += loss.item()
    tot_train_loss = train_loss/len(train_dataloader)

    model.eval()
    val_loss = 0.0
    with torch.no_grad():
        for batch_idx, (x, y) in enumerate(valid_dataloader):
            x, y = x.float().to(device), y.float().to(device)
            y_pred = model(x)
            loss = criterion(y_pred, y)
            val_loss += loss.item()
    tot_valid_loss = val_loss/len(valid_dataloader)
    
    if (epoch+1) % 5 == 0:
        print('Epoch: [{}/{}] | Loss: {:.4f} | Validation Loss: {:.4f}'.format(epoch+1, num_epochs, tot_train_loss, tot_valid_loss))

print("Training finished.")

