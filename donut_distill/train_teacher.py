from transformers import (
    DonutProcessor,
    VisionEncoderDecoderModel,
    VisionEncoderDecoderConfig,
)
from donut_distill.donut_dataset import DonutDataset
from torch.utils.data import DataLoader
import numpy as np
import torch
import wandb
from tqdm import tqdm
from datetime import datetime
from pathlib import Path
import math
from torch.optim.lr_scheduler import LambdaLR
import donut_distill.config as CONFIG
from donut_distill.evaluate import evaluate, evaluate_generation_configs
from transformers import GenerationConfig

TOKENIZERS_PARALLELISM = False

def prepare_dataloader(model, processor):
    train_dataset = DonutDataset(
        dataset_name_or_path=CONFIG.DATASET,
        processor=processor,
        model=model,
        max_length=CONFIG.MAX_LENGTH,
        split=CONFIG.DATASET_NAME_TRAINING,
        task_start_token="<s_funsd>",
        sort_json_key=False,  # cord dataset is preprocessed, so no need for this
    )

    val_dataset = DonutDataset(
        dataset_name_or_path=CONFIG.DATASET,
        processor=processor,
        model=model,
        max_length=CONFIG.MAX_LENGTH,
        split=CONFIG.DATASET_NAME_VALIDATE,
        task_start_token="<s_funsd>",
        sort_json_key=False,  # cord dataset is preprocessed, so no need for this
    )

    train_dataloader = DataLoader(
        train_dataset,
        batch_size=CONFIG.TRAIN_BATCH_SIZES,
        shuffle=True,
        num_workers=CONFIG.NUM_WORKERS,
    )
    val_dataloader = DataLoader(
        val_dataset,
        batch_size=CONFIG.VAL_BATCH_SIZES,
        shuffle=False,
        num_workers=CONFIG.NUM_WORKERS,
    )

    return train_dataloader, val_dataloader


def cosine_scheduler(optimizer, training_steps, warmup_steps):
    def lr_lambda(current_step):
        if current_step < warmup_steps:
            return current_step / max(1, warmup_steps)
        progress = current_step - warmup_steps
        progress /= max(1, training_steps - warmup_steps)
        return max(0.0, 0.5 * (1.0 + math.cos(math.pi * progress)))

    return LambdaLR(optimizer, lr_lambda)


def train():
    donut_config = VisionEncoderDecoderConfig.from_pretrained(CONFIG.MODEL_ID)
    donut_config.encoder.image_size = CONFIG.INPUT_SIZE
    donut_config.decoder.max_length = CONFIG.MAX_LENGTH

    processor = DonutProcessor.from_pretrained(CONFIG.MODEL_ID)
    model = VisionEncoderDecoderModel.from_pretrained(
        CONFIG.MODEL_ID, config=donut_config
    )

    processor.image_processor.size = CONFIG.INPUT_SIZE[::-1]
    processor.image_processor.do_align_long_axis = False

    train_dataloader, val_dataloader = prepare_dataloader(model, processor)

    model.config.pad_token_id = processor.tokenizer.pad_token_id
    model.config.decoder_start_token_id = processor.tokenizer.convert_tokens_to_ids(
        [""]
    )[0]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)

    # Optimizer and Scheduler
    optimizer = torch.optim.Adam(model.parameters(), lr=CONFIG.LR)
    if int(CONFIG.MAX_EPOCHS) > 0:
        max_iter = (CONFIG.MAX_EPOCHS * len(train_dataloader.dataset)) / (
            CONFIG.TRAIN_BATCH_SIZES
            * torch.cuda.device_count()
            * CONFIG.NUM_NODES
        )

    if int(CONFIG.MAX_STEPS) > 0:
        max_iter = (
            min(CONFIG.MAX_STEPS, max_iter)
            if max_iter is not None
            else CONFIG.MAX_STEPS
        )
    assert max_iter is not None
    scheduler = cosine_scheduler(optimizer, max_iter, CONFIG.WARMUP_STEPS)

    # Logger
    wandb.init(
        project="donut-funsd",
        name="5 datapoints",
        config={
            "learning_rate": CONFIG.LR,
            "architecture": "Donut",
            "dataset": "funsd",
            "epochs": CONFIG.MAX_EPOCHS,
            "gradient_clip_val": CONFIG.GRADIENT_CLIP_VAL,
        },
    )

    # Create directories for model and processor
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    model_dir = Path(CONFIG.RESULT_PATH) / f"donut_{timestamp}" / "model"
    processor_dir = Path(CONFIG.RESULT_PATH) / f"donut_{timestamp}" / "processor"

    scaler = torch.amp.GradScaler("cuda")
    best_val_metric = float("inf")
    steps = 0

    for epoch in range(CONFIG.MAX_EPOCHS):
        # Training phase
        model.train()
        losses = []
        for batch in tqdm(train_dataloader, desc=f"Training Epoch {epoch+1}"):
            pixel_values, labels, answers = batch
            pixel_values = pixel_values.to(device)
            labels = labels.to(device)

            with torch.autocast(device_type="cuda"):
                outputs = model(pixel_values, labels=labels)
                loss = outputs.loss
            optimizer.zero_grad()
            # loss.backward()
            scaler.scale(loss).backward()
            torch.nn.utils.clip_grad_norm_(
                model.parameters(), CONFIG.GRADIENT_CLIP_VAL
            )
            scaler.step(optimizer)
            scaler.update()
            # optimizer.step()
            losses.append(loss.item())

            # Log training metrics
            wandb.log({"train/loss": loss.item()}, step=steps)
            steps += 1

        avg_train_loss = np.mean(losses)

        eval_results = evaluate_generation_configs(
            model=model,
            processor=processor,
            device=device,
            val_dataloader=val_dataloader,
            generationsconfigs=[
                ("Nucleus K, p=0.95 k=40", GenerationConfig(
                    do_sample=True,
                    top_k=40,
                    top_p=0.95,
                )),
                ("Nucleus, p=0.94", GenerationConfig(
                    do_sample=True,
                    top_p=0.94,
                    top_k=0
                )),
                ("Beam ngrams, num=5 ngrams=8", GenerationConfig(
                    num_beams=5,
                    no_repeat_ngram_size=8,
                    early_stopping=True,
                )),
                ("Greedy", GenerationConfig(
                )),
            ]
        )

        log_data = { "train/avg_loss": avg_train_loss }
        for eval_result in eval_results:
            log_data.update(eval_result)

        wandb.log(
            log_data,
            step=steps,
        )

        # wandb.log(
        #     {
        #         "train/avg_loss": avg_train_loss,
        #         "validate/f1": eval_results["f1"],
        #         "validate/recall": eval_results["recall"],
        #         "validate/precision": eval_results["precision"],
        #     },
        #     step=steps,
        # )

        # if eval_results["f1"] < best_val_metric:
        #     print("Saving Model!")
        #     best_val_metric = eval_results["f1"]
        #     model.save_pretrained(model_dir)
        #     processor.save_pretrained(processor_dir)

        scheduler.step()


if __name__ == "__main__":
    train()
