import sklearn
import torch
from argparse import ArgumentParser
import os
import torchvision
os.environ["OMP_NUM_THREADS"] = "1"
from torchvision import models
from torch import nn
import time
import numpy as np
from torchvision import transforms
from torch.utils.data import DataLoader
from sklearn.metrics import confusion_matrix
import seaborn as sn
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import tensorflow as tf
import cv2
from torchvision.utils import save_image
import matplotlib.pyplot as plt
from big_nephro_dataset import YAML10YBiosDataset
from big_nephro_dataset import YAML10YBiosDatasetFluo
from big_nephro_dataset import YAML10YBiosDatasetAllPpb
from big_nephro_dataset import YAML10YDataset
from torch.utils.tensorboard import SummaryWriter
from sklearn import metrics
import torch
import shutil
import itertools
from sklearn.metrics import ConfusionMatrixDisplay
import wandb
import random



def save_ckp(state, checkpoint_dir):
    f_path = os.path.join(checkpoint_dir, 'checkpoint.pt')
    torch.save(state, f_path)


#PARTE 1: CONFUSION MATRIX 
class ConfusionMatrix:
    def __init__(self, num_classes):
        self.conf_matrix = np.zeros((num_classes, num_classes), int)

    def update_matrix(self, out, target):
        # I'm sure there is a better way to do this
        for j in range(len(target)):
            self.conf_matrix[out[j].item(), target[j].item()] += 1

    def get_metrics(self):
        samples_for_class = np.sum(self.conf_matrix, 0)
        diag = np.diagonal(self.conf_matrix)

        acc = np.sum(diag) / np.sum(samples_for_class)
        w_acc = np.divide(diag, samples_for_class)
        w_acc = np.mean(w_acc)

        return acc, w_acc

#PARTE 2: RESNET 

#Usa resnet pretrinata, toglie gli ultimi 2 layers che sono quelli di classificazione di resnet

class MyResnet(nn.Module):
    def __init__(self, net='resnet18', pretrained=True, num_classes=1, dropout_flag=True):
        super(MyResnet, self).__init__()
        self.dropout_flag = dropout_flag
        if net == 'resnet18':
            resnet = models.resnet18(pretrained)
            bl_exp = 1
        elif net == 'resnet34':
            resnet = models.resnet34(pretrained)
            bl_exp = 1
        elif net == 'resnet50':
            resnet = models.resnet50(pretrained)
            bl_exp = 4
        elif net == 'resnet101':
            resnet = models.resnet101(pretrained)
            bl_exp = 4
        elif net == 'resnet152':
            resnet = models.resnet152(pretrained)
            bl_exp = 4
      
        else:
            raise Warning("Wrong Net Name!!")

        self.resnet = nn.Sequential(*(list(resnet.children())[:-2]))

      
        self.avgpool = nn.AdaptiveAvgPool2d(output_size=1)
        self.maxpool = nn.AdaptiveMaxPool2d(output_size=1)

      
        
        if self.dropout_flag:
            self.dropout = nn.Dropout(0.2)
        n_features = 512 * bl_exp * 2
        self.first_fc = nn.Sequential(nn.Linear(n_features, n_features * 2),
                                      nn.BatchNorm1d(num_features=n_features * 2),
                                      nn.ReLU(inplace=True))
        self.second_fc = nn.Sequential(nn.Linear(n_features * 2, n_features * 2),
                                       nn.BatchNorm1d(num_features=n_features * 2),
                                       nn.ReLU(inplace=True))
        self.last_fc = nn.Linear(n_features * 2, num_classes)

    
    #DISACCOPPIAMENTO PER FLUO
    
    def forward(self, x):
        batch_size = x.size(0)
        x = x.view(x.size(0), x.size(1), x.size(2), x.size(3))
        x = self.resnet(x.float())
        x = x.view(batch_size, x.size(1), x.size(2), x.size(3))
        #print("La shape di x prima di permutare: {0} ".format(x.shape))
        #x = x.permute(0, 2, 1, 3)
        #print("La shape di x dopo aver permutato: {0} ".format(x.shape))
        avg_x = self.avgpool(x)
        max_x = self.maxpool(x)
        x = torch.cat((avg_x, max_x), dim=1)
        #print("La shape di x dopo cat: {0} ".format(x.shape))
        x = x.view(x.size(0), -1)
        #print("La shape di x dopo la view: {0} ".format(x.shape))
        if self.dropout_flag:
            x = self.dropout(x)
        x = self.first_fc(x)
        x = self.second_fc(x)
        if self.dropout_flag:
            x = self.dropout(x)
        x = self.last_fc(x)
        return x
    

#PARTE 3: DENSNET
#https://www.youtube.com/watch?v=hSC_0S8Zf9s
class MyDensenet(nn.Module):
    def __init__(self, net='densenet', pretrained=True, num_classes=1, dropout_flag=True):
        super(MyDensenet, self).__init__()
        self.dropout_flag = dropout_flag
        if net == 'densenet':
            densenet = models.densenet121(pretrained)
        else:
            raise Warning("Wrong Net Name!!")
        self.densenet = nn.Sequential(*(list(densenet.children())[0]))
        self.relu = nn.ReLU()
        self.avgpool = nn.AdaptiveAvgPool2d((512 * 2))
        if self.dropout_flag:
            self.dropout = nn.Dropout(0.5)
        self.last_fc = nn.Linear(1024, num_classes)

    def forward(self, x):
        x = self.densenet(x)
        x = self.relu(x)
        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        if self.dropout_flag:
            x = self.dropout(x)
        x = self.last_fc(x)
        return x


class NefroNet():
    def __init__(self, years, net, input_patches, preprocess_type, num_classes, num_epochs, l_r, batch_size, n_workers, job_id, weights, images_type):
        # Hyper-parameters
        self.years = years
        self.net = net
        self.input_patches = input_patches
        self.preprocess_type = preprocess_type
        self.num_classes = num_classes
        self.num_epochs = num_epochs
        self.learning_rate = l_r
        self.batch_size = batch_size
        self.n_workers = n_workers
        self.job_id = job_id
        self.weights = weights
        self.thresh = 0.5
        self.models_dir = "//homes//grosati//Medical//MODELS//"
        #self.models_dir_pollastri = '/homes/grosati/Medical/MODELS/pollastri'
        self.models_dir_pollastri = '/nas/softechict-nas-2/fpollastri/big_nephro/5Y/MODELS'
        self.best_acc = 0.0
        #self.nname = self.net + f'_{self.years}Y_' + str(job_id)
        self.nname = self.net + '_5Y_' + str(job_id)
        self.images_type=images_type

        #dname = f'/nas/softechict-nas-2/fpollastri/data/big_nephro/big_nephro_{self.years}Y_bios_dataset.yml'
        dname= '/nas/softechict-nas-2/fpollastri/data/big_nephro/big_nephro_5Y_bios_dataset.yml'
        #dname= '/nas/softechict-nas-2/fpollastri/data/big_nephro/big_nephro_bios_dataset.yml'
        dataset_type = 'patches'

        dataset_mean = (0.813, 0.766, 0.837)
        dataset_std = (0.148, 0.188, 0.124)
    
        dataset_mean_fluo_IgA_hand = (90.14926028, 90.14957433, 90.1492549)
        dataset_std_fluo_IgA_hand = (285.92956238, 285.92946316, 285.92956381)
        
        if preprocess_type == 'random':
            preprocess_fn = transforms.RandomResizedCrop(size=(256, 512), scale=(.5, 1.0), ratio=(2., 2.))
            preprocess_fn_fluo = transforms.RandomResizedCrop(size=(256, 512), scale=(.5, 1.0), ratio=(2., 2.))
        elif preprocess_type == 'crop':
            preprocess_fn = transforms.RandomCrop(512, pad_if_needed=True, fill=255)
        elif preprocess_type == 'whole_patch':
            preprocess_fn = transforms.Compose([transforms.RandomCrop((1000, 2000), pad_if_needed=True, fill=255), transforms.Resize(size=(256, 512))])
        elif preprocess_type == 'big_whole_patch':
            preprocess_fn = transforms.Compose([transforms.RandomCrop((1000, 2000), pad_if_needed=True, fill=255), transforms.Resize(size=(512, 1024))])
        elif preprocess_type == 'glomeruli':
            dataset_type = 'glomeruli'
            dataset_mean = (0.746, 0.673, 0.784)
            dataset_std = (0.175, 0.217, 0.143)
            preprocess_fn = transforms.Resize(size=(256, 256))
        elif preprocess_type == 'big_glomeruli':
            dataset_type = 'glomeruli'
            dataset_mean = (0.746, 0.673, 0.784)
            dataset_std = (0.175, 0.217, 0.143)
            preprocess_fn = transforms.Resize(size=(512, 512))
        else:
            raise ValueError("unknown preprocessing technique")
    #FLUO
        if self.images_type == "fluo":
            custom_training_transforms_fluo = transforms.Compose([
                
               
         
                transforms.RandomHorizontalFlip(),
                transforms.RandomVerticalFlip(),
                transforms.ToTensor(),
                preprocess_fn_fluo,
                transforms.Resize((772, 1040)),
                
            ])
            inference_transforms_fluo = transforms.Compose([
            
                transforms.ToTensor(),
                #preprocess_fn_fluo,
                transforms.Resize((772, 1040)),
                
            ])
            fluo_norm = transforms.Compose([
                transforms.Normalize(dataset_mean_fluo_IgA_hand, dataset_std_fluo_IgA_hand),
            ])

         
        if self.images_type == "fluo":
            dataset = YAML10YBiosDatasetFluo(dataset=dname, crop_type=dataset_type, patches_per_bio=self.input_patches, transforms=custom_training_transforms_fluo, augmentation_transform = None, fluo_transform=fluo_norm, split=['training'])
            #validation_dataset = YAML10YDataset(dataset=dname, patches_per_bio=max(16, self.input_patches), transforms=inference_transforms, split=['validation'])
            test_dataset = YAML10YBiosDatasetFluo(dataset=dname, crop_type=dataset_type, patches_per_bio=max(16, self.input_patches * 2), transforms=inference_transforms_fluo, fluo_transform=fluo_norm, augmentation_transform = None, split=['test'])

        if self.net == 'densenet': 
            self.n = MyDensenet(net=self.net, num_classes=self.num_classes).to('cuda')
        else:
            self.n = MyResnet(net=self.net, num_classes=self.num_classes).to('cuda')

        self.data_loader = DataLoader(dataset,batch_size=self.batch_size,shuffle=True,num_workers=self.n_workers,drop_last=True,pin_memory=False)
        
        #self.validation_data_loader = DataLoader(validation_dataset,batch_size=self.batch_size,shuffle=False,num_workers=self.n_workers,drop_last=False,pin_memory=True)
        
        self.test_data_loader = DataLoader(test_dataset,batch_size=1,shuffle=False,num_workers=self.n_workers,drop_last=False,pin_memory=False)
        
        # Loss and optimizer
        if self.num_classes == 1:
            
            if self.images_type == "fluo":
                dict_label=dataset.get_labels()
                size_0 = dict_label.get('0')
                size_1 = dict_label.get('1')
                t=torch.tensor([size_0/size_1]).to('cuda')
                self.criterion = nn.BCEWithLogitsLoss(pos_weight=t)
            
        else:
            c1_w = get_probabilities(self.data_loader)
            c0_w = 1.0 - c1_w
            c1_w = 1.0 / c1_w
            c0_w = 1.0 / c0_w
            class_w = torch.tensor([c0_w, c1_w], device='cuda')
       
        self.optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, self.n.parameters()), lr=self.learning_rate)
        self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, mode='min', verbose=True)

    def freeze_layers(self, freeze_flag=True, nl=0):
        if nl:
            l = list(self.n.resnet.named_children())[:-nl]
        else:
            l = list(self.n.resnet.named_children())
      
        for name, child in l:
            for param in child.parameters():
                param.requires_grad = not freeze_flag
    
    
    #TRAIN_DISACCOPPIATO (SOLO PER FLUO)
    def train(self):
       
        wandb.init(
                 # set the wandb project where this run will be logged
            project="nephro",
            name=f"experiment_{self.nname}", 
               
            config={
                
                "learning_rate": 0.01,
                "architecture": "Resnet-18",
                "dataset": "big_nephro",
                "epochs": 80,
                        }
            )

        for epoch in range(self.num_epochs):
            self.n.train()
            losses = []
            start_time = time.time()
            for i, (x, target) in enumerate(self.data_loader):
                if os.environ['SLURM_NODELIST'] == 'aimagelab-srv-00':
                    print(f'doing batch #{i + 1}/{len(self.data_loader)}')
                   
                # compute output
                x = x.to('cuda')
                if self.num_classes == 1:
                    target = target.to('cuda', torch.float)
                    if self.weights:
                        self.criterion.weight = get_weights(target)

                else:
                    target = target.to('cuda', torch.long)
             
                output = torch.squeeze(self.n(x), -1)
                loss = self.criterion(output, target)
                losses.append(loss.item())
                # compute gradient and do SGD step
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

            print('Epoch: ' + str(epoch) + ' | loss: ' + str(np.mean(losses)) + ' | time: ' + str(
                time.time() - start_time))
            print('test: ')

            metrics = self.eval(self.test_data_loader)        
            # Define which metrics to plot against that x-axis
            wandb.define_metric("Loss/Train", step_metric='epoch')
            wandb.define_metric("metrics/AUC", step_metric='epoch')
            wandb.define_metric("metrics/Recall", step_metric='epoch')
            wandb.define_metric("metrics/Specificity", step_metric='epoch')
            wandb.define_metric("metrics/Precision", step_metric='epoch')
            
            wandb.log({'Loss/Train': np.mean(losses), 'epoch':epoch})
            wandb.log({'metrics/AUC': metrics[0], 'epoch':epoch})
            wandb.log({'metrics/Recall': metrics[2], 'epoch':epoch})
            wandb.log({'metrics/Specificity': metrics[3], 'epoch':epoch})
            wandb.log({'metrics/Precision': metrics[4], 'epoch':epoch})
            class_names=['0','1']
            
            #metrics[5]=y_true calcolato in eval
            #metrics[6]=y_pred calcolato in eval
            wandb.log({"conf_mat" : wandb.plot.confusion_matrix(probs=None,
                            y_true=metrics[5], preds=metrics[6],
                            class_names=class_names)})

            checkpoint = {
                'epoch': epoch + 1,
                'state_dict': self.n.state_dict(),
                'optimizer': self.optimizer.state_dict(),
                'best_acc': self.best_acc
            }
            save_ckp(checkpoint, self.models_dir)
            
            if metrics[0] > self.best_acc and epoch > 10:
                print("SAVING MODEL")
                self.save()
                self.best_acc = metrics[0]
            self.scheduler.step(np.mean(losses))
            if self.learning_rate // self.optimizer.param_groups[0]['lr'] >= 10 ** 4:
                print("Training process will be stopped now due to the low learning rate reached")
                self.save()
                return
            
    
        wandb.finish()
    #EVAL_DISACCOPPIATO (SOLO PER FLUO)
    def eval(self, d_loader=None):
        if d_loader is None:
            d_loader = self.test_data_loader
        with torch.no_grad():
            sigm = nn.Sigmoid()
            sofmx = nn.Softmax(dim=-1)
           
            self.n.eval()

            preds = np.zeros(len(d_loader.dataset))
            gts = np.zeros(len(d_loader.dataset))
            start_time = time.time()
            for i, (x, target) in enumerate(d_loader):
                
                x = x.to('cuda')
                output = torch.squeeze(self.n(x))
                if self.num_classes == 1:
                    target = target.to('cuda', torch.float)
                    check_output = sigm(output)
                    # target = (target > self.thresh).float()
                    target = (target == 1.).float()
                  
                else:
                    target = target.to('cuda', torch.long)
                    check_output = sofmx(output)
                    
                gts[i * d_loader.batch_size:i * d_loader.batch_size + len(target)] = target.to('cpu')
                preds[i * d_loader.batch_size:i * d_loader.batch_size + len(target)] = check_output.to('cpu')
            
                
            if self.num_epochs == 0:
                threshes = np.arange(100) / 100.0
            else:
                threshes = [self.thresh]
            for t in threshes:
                print(f'\nthresh: {t}')
               
                bin_preds = np.where(preds > t, 1., 0.)
                tr_targets = gts * 2 - 1
                trues = sum(bin_preds)
                y_true = gts
                y_pred = bin_preds
                tr_trues = sum(bin_preds == tr_targets)
                g_trues = sum(gts)
                pr = tr_trues / (trues + 10e-5)
                rec = tr_trues / g_trues
                spec = (sum(gts == bin_preds) - tr_trues) / sum(gts == 0)
                fscore = (2 * pr * rec) / (pr + rec + 10e-5)
                acc = np.mean(gts == bin_preds).item()
                auc = metrics.roc_auc_score(gts, preds)
                stats_string = f'Acc: {acc:.3f} | AUC: {auc:.3f} | F1 Score: {fscore:.3f} | Precision: {pr:.3f} | Recall: {rec:.3f} | Specificity: {spec:.3f} | Trues: {trues:.0f} | Correct Trues: {tr_trues:.0f} | ' \
                               f'Ground Truth Trues: {g_trues:.0f} | time: {(time.time() - start_time):.3f}'
                print(stats_string)
        return auc, fscore, rec, spec, pr, y_true, y_pred
    
    def inference(self, d_loader=None, DA=False, n_reps=20):
        if d_loader is None:
            d_loader = self.test_data_loader
        if DA:
            d_loader.dataset.transforms = self.data_loader.dataset.transforms
        with torch.no_grad():
            sigm = nn.Sigmoid()
            sofmx = nn.Softmax(dim=-1)
            self.n.eval()

    
            preds = np.zeros(len(d_loader.dataset))
            gts = np.zeros(len(d_loader.dataset))
            start_time = time.time()
            for rep in range(n_reps):
                print(f'now doing rep {rep + 1}/{n_reps}')
                for i, (x, target, img_name) in enumerate(d_loader):
                
                    
                    # compute output
                    x = x.to('cuda')
                    output = torch.squeeze(self.n(x))
                    if self.num_classes == 1:
                        target = target.to('cuda', torch.float)
                        print(target)
                        check_output = sigm(output)
                        # res = (check_output > self.thresh).float()
                        target = (target == 1.).float()
                    else:
                        target = target.to('cuda', torch.long)
                        check_output = sofmx(output)
                        # check_output, res = torch.max(check_output, 1)
                        # res = (check_output[:, 1] > self.thresh).int()
                    gts[i * d_loader.batch_size:i * d_loader.batch_size + len(target)] += target.to('cpu').numpy()
                    preds[i * d_loader.batch_size:i * d_loader.batch_size + len(target)] += check_output.to('cpu').numpy()

            gts /= n_reps
            preds /= n_reps
            if self.num_epochs == 0:
                threshes = np.arange(100) / 100.0
            else:
                threshes = [self.thresh]
            for t in threshes:
                print(f'\nthresh: {t}')
                # bin_preds = np.where(preds > self.thresh, 1., 0.)
                bin_preds = np.where(preds > t, 1., 0.)
                tr_targets = gts * 2 - 1
                trues = sum(bin_preds)
                tr_trues = sum(bin_preds == tr_targets)
                g_trues = sum(gts)
                pr = tr_trues / (trues + 10e-5)
                rec = tr_trues / g_trues
                spec = (sum(gts == bin_preds) - tr_trues) / sum(gts == 0)
                fscore = (2 * pr * rec) / (pr + rec + 10e-5)
                acc = np.mean(gts == bin_preds).item()
                auc = metrics.roc_auc_score(gts, preds)

                stats_string = f'Acc: {acc:.3f} | AUC: {auc:.3f} | F1 Score: {fscore:.3f} | Precision: {pr:.3f} | Recall: {rec:.3f} | Specificity: {spec:.3f} | Trues: {trues:.0f} | Correct Trues: {tr_trues:.0f} | ' \
                               f'Ground Truth Trues: {g_trues:.0f} | time: {(time.time() - start_time):.3f}'
                print(stats_string)
            import csv
            names = list(d_loader.dataset.bios.keys())
            with open(f'/homes/grosati/Medical/big_nephro_results_{self.job_id}.csv', 'w') as csv_file:
                writer = csv.writer(csv_file)
                for i in range(len(names)):
                    writer.writerow([names[i], preds[i], gts[i]])

        return auc, fscore, rec, spec, pr

    def validate(self):
        with torch.no_grad():
            sigm = nn.Sigmoid()
            sofmx = nn.Softmax(dim=1)
            trues = 0
            tr_trues = 0
            acc = 0
            self.n.eval()

            start_time = time.time()
            for i, (x, target, img_name) in enumerate(self.validation_data_loader):
               
                # compute output
                
                x = x.to('cuda')
                output = torch.squeeze(self.n(x))
                if self.num_classes == 1:
                    target = target.to('cuda', torch.float)
                    check_output = sigm(output)
                    res = (check_output > self.thresh).float()
                else:
                    target = target.to('cuda', torch.long)
                    check_output = sofmx(output)
                    check_output, res = torch.max(check_output, 1)

                tr_target = target * 2
                tr_target = tr_target - 1
                tr_trues += sum(res == tr_target).item()
                trues += sum(res).item()
                acc += sum(res == target).item()

            pr = tr_trues / (trues + 10e-5)
            rec = tr_trues / 100
            fscore = (2 * pr * rec) / (pr + rec + 10e-5)
            stats_string = 'Test set = Acc: ' + str(acc / 500.0) + ' | F1 Score: ' + str(
                fscore) + ' | Precision: ' + str(
                pr) + ' | Recall: ' + str(rec) + ' | Trues: ' + str(trues) + ' | Correct Trues: ' + str(
                tr_trues) + ' | time: ' + str(time.time() - start_time)
            print(stats_string)

    def find_stats(self):
        mean = 0.
        std = 0.
        nb_samples = 0.
        b = 0
        for data, _, _ in self.data_loader:
            b += 1
            print(b)
            batch_samples = data.size(0)
            data = data.view(batch_samples, data.size(1), -1)
            mean += data.mean(2).sum(0)
            std += data.std(2).sum(0)
            nb_samples += batch_samples

        mean /= nb_samples
        std /= nb_samples
        print("\ntraining")
        print("mean: " + str(mean) + " | std: " + str(std))

    def save(self):
        try:
            torch.save(self.n.state_dict(), os.path.join(self.models_dir, self.nname + '_net.pth'))
            torch.save(self.optimizer.state_dict(), os.path.join(self.models_dir, self.nname + '_opt.pth'))
            print("model weights successfully saved")
        except Exception:
            print("Error during Saving")

    def load(self):
        self.n.load_state_dict(torch.load(os.path.join(self.models_dir_pollastri, self.nname + '_net.pth')))
        self.optimizer.load_state_dict(torch.load(os.path.join(self.models_dir_pollastri, self.nname + '_opt.pth')))
        print("model weights successfully loaded")

    def load_old_ckpt(self, ckpt_name='_old'):
        self.n.load_state_dict(torch.load(os.path.join(self.models_dir, self.lbl_name + '_net' + ckpt_name + '.pth')))
        # self.optimizer.load_state_dict(torch.load(os.path.join(self.models_dir, self.lbl_name + '_opt' + ckpt_name + '.pth')))
        print("model old weights successfully loaded")


    def see_imgs(self):
        cntr = 0
        for data in self.eval_data_loader:
            cntr += 1
            save_image(data[0].float(),
                       '/homes/grosati/aug_images/' + os.path.basename(data[2][0])[:-4] + '.png',
                       nrow=1, pad_value=0)
            print("img saved")


def get_weights(target):
    # 0.9 for True, 0.2 for Falses
    weights = target * 0.7
    weights += 0.2
    return weights


def get_probabilities(dl):
    #counter = sum(dl.dataset.lbls)
    for _, l, _ in dl:
         counter += sum(l).item()

    return counter / len(dl.dataset)


def show_cam_on_image(img, mask, name):
    heatmap = cv2.applyColorMap(np.uint8(255 * mask), cv2.COLORMAP_JET)
    heatmap = np.float32(heatmap) / 255
    cam = heatmap + np.moveaxis(np.float32(img.cpu()), 0, -1)
    cam = cam / np.max(cam)
    cv2.imwrite('/homes/grosati/nefro_GradCam/' + name + '_cam.png', np.uint8(255 * cam))


def plot(img):
    return
    plt.figure()
    # plt.imshow(nefro_4k_and_diapo.denormalize(img))
    plt.imshow(img)
    plt.show(block=False)



if __name__ == '__main__':
    

    parser = ArgumentParser()

    parser.add_argument('--network', default='resnet18')
    parser.add_argument('--patches_per_bio', type=int, default=4, help='number of epochs to train')
    parser.add_argument('--preprocess', default='random', choices=['random', 'crop', 'whole_patch', 'big_whole_patch', 'glomeruli', 'big_glomeruli'])
    parser.add_argument('--classes', type=int, default=1, help='number of classes in the task')
    parser.add_argument('--load_epoch', type=int, default=0, help='load pretrained models')
    parser.add_argument('--workers', type=int, default=4, help='number of data loading workers')
    parser.add_argument('--batch_size', type=int, default=8, help='batch size during the training')
    parser.add_argument('--learning_rate', type=float, default=0.01, help='learning rate')
    parser.add_argument('--epochs', type=int, default=100, help='number of epochs to train')
    parser.add_argument('--SRV', action='store_true', help='is training on remote server')
    parser.add_argument('--weighted', action='store_true', help='add class weights')
    parser.add_argument('--job_id', type=str, default='', help='slurm job ID')
    parser.add_argument('--images_type', type=str, default='fluo', help='')
    opt = parser.parse_args()
    print(opt)

    n = NefroNet(years=5, net=opt.network, input_patches = opt.patches_per_bio, preprocess_type=opt.preprocess, num_classes=opt.classes, num_epochs=opt.epochs, batch_size=opt.batch_size,
                 l_r=opt.learning_rate, n_workers=opt.workers, job_id=opt.job_id, weights=opt.weighted, images_type=opt.images_type)
    
  
           
    if opt.epochs > 0:  
        
            n.train()
            n.eval()
    # TODO go to 5 years prediction