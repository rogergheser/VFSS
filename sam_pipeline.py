import argparse
import torch
import torchvision
import torch.utils.data as data
import matplotlib.pyplot as plt
import logging # TODO Configure logging
import numpy as np
import yaml
import cv2
import os   
import copy

from datasets.dataset_vars import (
    ADE20K_SEM_SEG_FULL_CATEGORIES as ADE20K_CATEGORIES
)
from utils.data import (
    ADE20KDataset,
    QualitativeDataset,
    Coco
)
from utils.utilsSAM import (
    post_processing,
    recompose_image,
    filter_masks,
    annotate_image
)
from torchvision import transforms as transform
from models.alphaClip import AlphaClip
from models.SAM import SAMSegmenter
from tqdm import tqdm

# evaluation using SAN evaluator
import sys
sys.path.append('SAN')
os.environ['DETECTRON2_DATASETS'] = os.getcwd() + '/datasets'
from SAN.custom_evaluator import CustomSemSegEvaluator
from detectron2.modeling.meta_arch.build import build_model
from SAN.eval_net import setup
from detectron2.engine import default_argument_parser

class Evaluator:
    def __init__(self,
                 sam: SAMSegmenter,
                 clip: AlphaClip,
                 loader: data.DataLoader,
                 evaluator: CustomSemSegEvaluator,
                 device:str='cuda',
                 args: dict=None):
        """
        :param sam: SAMSegmenter instance
        :param clip: AlphaClip instance
        :param loader: DataLoader instance
        :param device: device to use
        """
        self.sam = sam
        self.clip = clip
        self.loader = loader
        self.evaluator = evaluator
        self.device = device
        self.save_predictions = args['output']['save_predictions']
        self.overlay = args['output']['overlay']
        self.out_path = args['output']['save_path']
        self.post_process = args['sam']['post_process']
        self.ade_voc = {}
        self.new_label_idx = 0

        if self.save_predictions:
            os.makedirs(self.out_path, exist_ok=True)

        for i, category in enumerate(ADE20K_CATEGORIES):
            keys = category["name"].split(", ")
            self.new_label_idx += 1
            for key in keys:
                if key not in self.ade_voc:
                    self.ade_voc[key] = category["trainId"]

    def eval(self):
        os.makedirs(self.out_path, exist_ok=True)
        loop = tqdm(self.loader, total=len(self.loader))
        print("-"*90)
        print("Starting evaluation")
        for i, batch in enumerate(loop):
            image = batch['image'].squeeze(0).to(self.device)
            vocabulary = batch['vocabulary']
            json_label = batch['label'] # unused in our experiments

            masks = self.sam.predict_mask(image)
            masks, _ = filter_masks(masks)
            # These masks are guaranteed to remain intact, preserving correct shapes
            original_masks = copy.deepcopy(masks) 

            images, masks = post_processing(masks, image, post_processing=self.post_process)
            
            logits = self.clip.classify(images, masks, vocabulary)
            predictions = torch.argmax(logits, dim=1)
            text_predictions = [vocabulary[pred.item()][0] for pred in predictions]
            semseg = self.add_labels(image, text_predictions, original_masks)

            if self.save_results:
                overlay = recompose_image(image.cpu().numpy(), masks, overlay=self.overlay)
                self.save_interpretable_results(overlay.transpose(1, 2, 0), f'{self.out_path}/{i}.png', vocabulary, text_predictions, masks)

            output = [{'sem_seg': logits}]
            if self.evaluator is not None:
                self.evaluator.process(inputs=batch, outputs=output)

    def save_interpretable_results(self,
                                   overlay_img: np.ndarray,
                                   output_path: str,
                                   vocabulary: list[str],
                                   predictions: list[str],
                                   masks: list[dict]):
        """
        This function takes the original image, predictions and masks and recomposed the 
        segmentation map with the predictions overlayed on top of the image. 
        The final output is an RGB image.
        :param overlay_img: Original image
        :param output_path: Path to save the image
        :param vocabulary: List of words in the vocabulary
        :param predictions: List of predictions
        :param masks: List of masks
        """
        assert overlay_img.shape[-1] == 3, "Overlay image must have RGB channels last"

        overlay_img_copy = np.ascontiguousarray(overlay_img, dtype=np.uint8)
        # Get the dimensions of the image
        img_height, img_width = overlay_img_copy.shape[:2]
        
        # Define a font scale based on the image dimensions
        font_scale = min(img_width, img_height) / 700.0  # Adjust the divisor for desired scale

        for i, mask in enumerate(masks):
            x, y, w, h = mask['bbox']
            origin = (x + int(w/2) - 45, y + int(h/2))

            # Overlay the text
            cv2.putText(
                overlay_img_copy,
                predictions[i], 
                origin,
                cv2.FONT_HERSHEY_SIMPLEX,
                fontScale=font_scale,
                color=(255, 255, 255),
                thickness=1,
                lineType=cv2.LINE_AA
            )

        cv2.imwrite(output_path, overlay_img_copy)

        return overlay_img_copy

    def add_labels(self,
                   image: torch.Tensor, 
                   text_predictions: list[str],
                   masks: list[dict]) -> torch.Tensor:
        """
        This function takes the image, text predictions and masks and creates a semantic segmentation map.
        The final output is a tensor for evaluation.
        :param image: Original image
        :param text_predictions: List of text predictions
        :param masks: List of masks
        :return: Semantic segmentation map
        """
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
    
def get_san_model(args):
    detectron_args = default_argument_parser().parse_args()

    detectron_args.config_file = 'SAN/configs/san_clip_vit_res4_coco.yaml'
    detectron_args.eval_only = True
    detectron_args.opts = ['OUTPUT_DIR', args['output']['save_results'], 'MODEL.WEIGHTS', 'checkpoints/san_vit_b_16.pth', 'DATASETS.TEST', "('ade20k_full_sem_seg_val',)"]
    cfg = setup(detectron_args)
    san_model = build_model(cfg)

    return san_model, cfg

def main(dataset, args):
    sam = SAMSegmenter.from_args(args['sam'], device=args['device'])
    clip = AlphaClip.from_args(args['clip'], device=args['device'])
    loader = data.DataLoader(
        dataset, 
        batch_size=args['dataloader']['batch_size'],
        shuffle=args['dataloader']['shuffle'],)
    
    if args['dataset']['name'] == 'qualitative':
        quantitative_evaluator = None
    else:
        san_model, san_cfg = get_san_model()
        quantitative_evaluator=CustomSemSegEvaluator(san_model, args['dataset']['name'], False, san_cfg.OUTPUT_DIR)
    
    evaluator = Evaluator(sam, clip, loader, quantitative_evaluator, device=args['device'], args=args)
    evaluator.eval()
    evaluator.evaluator.evaluate()


if __name__ == '__main__':
    print("PyTorch version:", torch.__version__)
    print("Torchvision version:", torchvision.__version__)
    print("CUDA is available:", torch.cuda.is_available())
    
    parser = argparse.ArgumentParser(description='Semantic Segmentation with SAM')
    parser.add_argument('--config_file', type=str, default='configs/sam_cfg.yaml', help='Path to config file')
    # make an optional save results argument, which if present overrides the config file argument
    parser.add_argument('--save_results', action='store_true', help='Save the results of the evaluation')
    pyargs = parser.parse_args()

    with open(pyargs.config_file, 'r') as file:
        args = yaml.load(file, Loader=yaml.FullLoader)
    
    _transform = transform.Compose([
        # transform.Resize((args['dataset']['resize'], args['dataset']['resize'])),
        transform.PILToTensor(),
    ])

    dataset_name_to_class = {
        'qualitative': QualitativeDataset,
        'ade20k_full_sem_seg_val': ADE20KDataset,
        'coco_2017_test_stuff_sem_seg' : Coco
    }

    subset_percentage = args['dataset']['subset']
    
    dataset_name = args['dataset']['name']
    dataset_class = dataset_name_to_class[dataset_name]
    dataset = dataset_class.from_args(args, _transform)
    if subset_percentage:
        subset_size = int(len(dataset) * subset_percentage)
        sample_to_skip = len(dataset) // subset_size
        subset = torch.utils.data.Subset(dataset, range(0, len(dataset), sample_to_skip))

    if subset_percentage:
        main(subset, args)
    else:
        main(subset, args)