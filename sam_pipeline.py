import torch
import torchvision
import torch.utils.data as data
import matplotlib.pyplot as plt
import logging # TODO Configure logging
import numpy as np
import yaml
import cv2

from datasets.dataset_vars import (
    ADE20K_SEM_SEG_FULL_CATEGORIES as ADE20K_CATEGORIES
)
from utils.data import ADE20KDataset
from utils.utilsSAM import post_processing
from torchvision import transforms as transform
from models.alphaClip import AlphaClip
from models.SAM import SAMSegmenter
from tqdm import tqdm


class Evaluator:
    def __init__(self,
                 sam: SAMSegmenter,
                 clip: AlphaClip,
                 loader: data.DataLoader,
                 device:str='cuda'):
        """
        :param sam: SAMSegmenter instance
        :param clip: AlphaClip instance
        :param loader: DataLoader instance
        :param device: device to use
        """
        self.sam = sam
        self.clip = clip
        self.loader = loader
        self.device = device
        self.ade_voc = {}
        self.new_label_idx = 0
        for i, category in enumerate(ADE20K_CATEGORIES):
            keys = category["name"].split(", ")
            self.new_label_idx += 1
            for key in keys:
                if key not in self.ade_voc:
                    self.ade_voc[key] = category["trainId"]


    def eval(self):
        loop = tqdm(self.loader, total=len(self.loader))
        print("-"*90)
        print("Starting evaluation")
        for i, batch in enumerate(loop):
            image = batch['image'].squeeze(0).to(self.device)
            vocabulary = batch['vocabulary']
            json_label = batch['label']

            masks = self.sam.predict_mask(image)

            images, masks = post_processing(masks, image.type(torch.float32), post_processing='none')
            logits = self.clip.classify(images, masks, vocabulary)
            predictions = torch.argmax(logits, dim=1)
            text_predictions = [vocabulary[pred.item()] for pred in predictions]
            semseg = self.add_labels(image, text_predictions, masks)
            # assemble image
            # evaluate image

    def add_labels(self, image, text_predictions, masks):
        for text in text_predictions:
            if text not in self.ade_voc:
                self.ade_voc[text] = self.new_label_idx
                self.new_label_idx += 1
        newshape = image.shape
        newshape = (1, newshape[1], newshape[2])
        semseg = torch.zeros(newshape, dtype=torch.int32)
        # semseg size is (1, W, H)
        # mask['segmentation'] shape is (W x H)
        for text, mask in zip(text_predictions, masks):
            semseg[:, mask['segmentation']] = self.ade_voc[text]

        return semseg

def main(args):
    sam = SAMSegmenter.from_args(args['sam'], device=args['device'])
    clip = AlphaClip.from_args(args['clip'], device=args['device'])
    loader = data.DataLoader(
        ADE20KDataset(
            args['root'], 
            transform=transform.PILToTensor(),
            vocabulary='image_caption',
            ), batch_size=1, shuffle=False)
    
    evaluator = Evaluator(sam, clip, loader, device=args['device'])
    evaluator.eval()



if __name__ == '__main__':
    print("PyTorch version:", torch.__version__)
    print("Torchvision version:", torchvision.__version__)
    print("CUDA is available:", torch.cuda.is_available())
    
    with open('configs/sam_cfg.yaml', 'r') as file:
        args = yaml.load(file, Loader=yaml.FullLoader)
    
    main(args)