from transformers import (
    DonutProcessor,
    VisionEncoderDecoderModel,
    VisionEncoderDecoderConfig,
)
from donut_distill.donut_dataset import DonutDataset
from torch.utils.data import DataLoader
import torch
import wandb
from tqdm import tqdm
from datetime import datetime
from pathlib import Path
import math
from torch.optim.lr_scheduler import LambdaLR
import donut_distill.config as CONFIG
from donut_distill.evaluate import evaluate_docvqa
from transformers import GenerationConfig
from typing import List, Optional
import time

TOKENIZERS_PARALLELISM = False


# https://github.com/NielsRogge/Transformers-Tutorials/blob/master/Donut/DocVQA/Fine_tune_Donut_on_DocVQA.ipynb
def add_tokens(model, processor, list_of_tokens: List[str]):
    """
    Add tokens to tokenizer and resize the token embeddings
    """
    newly_added_num = processor.tokenizer.add_tokens(list_of_tokens)
    if newly_added_num > 0:
        model.decoder.resize_token_embeddings(len(processor.tokenizer))


def prepare_dataloader(model, processor):
    train_dataset = DonutDataset(
        dataset_name_or_path=CONFIG.DATASET,
        processor=processor,
        model=model,
        max_length=CONFIG.MAX_LENGTH,
        split=CONFIG.DATASET_NAME_TRAINING,
        task_start_token="<s_docvqa>",
        prompt_end_token="<s_answer>",
        sort_json_key=CONFIG.SORT_JSON_KEY,  # cord dataset is preprocessed, so no need for this
        task="docvqa",
    )

    val_dataset = DonutDataset(
        dataset_name_or_path=CONFIG.DATASET,
        processor=processor,
        model=model,
        max_length=CONFIG.MAX_LENGTH,
        split=CONFIG.DATASET_NAME_VALIDATE,
        task_start_token="<s_docvqa>",
        prompt_end_token="<s_answer>",
        sort_json_key=CONFIG.SORT_JSON_KEY,  # cord dataset is preprocessed, so no need for this
        task="docvqa",
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
        shuffle=True,
        num_workers=CONFIG.NUM_WORKERS,
    )

    return train_dataloader, val_dataloader


def prepare_model_and_processor(special_tokens: Optional[List[str]] = None):
    donut_config = VisionEncoderDecoderConfig.from_pretrained(CONFIG.MODEL_ID)
    donut_config.encoder.image_size = CONFIG.INPUT_SIZE
    donut_config.decoder.max_length = CONFIG.MAX_LENGTH

    processor = DonutProcessor.from_pretrained(CONFIG.MODEL_ID)
    model = VisionEncoderDecoderModel.from_pretrained(
        CONFIG.MODEL_ID, config=donut_config
    )

    if special_tokens:
        add_tokens(model, processor, special_tokens)

    processor.image_processor.size = CONFIG.INPUT_SIZE[::-1]
    processor.image_processor.do_align_long_axis = False

    return model, processor


def cosine_scheduler(optimizer, training_steps, warmup_steps):
    def lr_lambda(current_step):
        if current_step < warmup_steps:
            return current_step / max(1, warmup_steps)
        progress = current_step - warmup_steps
        progress /= max(1, training_steps - warmup_steps)
        return max(0.0, 0.5 * (1.0 + math.cos(math.pi * progress)))

    return LambdaLR(optimizer, lr_lambda)


def prepare_optimizer_and_scheduler(model, len_trainingsdata):
    optimizer = torch.optim.Adam(model.parameters(), lr=CONFIG.LR)
    if int(CONFIG.MAX_EPOCHS) > 0:
        max_iter = (CONFIG.MAX_EPOCHS * len_trainingsdata) / (
            CONFIG.TRAIN_BATCH_SIZES * torch.cuda.device_count() * CONFIG.NUM_NODES
        )

    if int(CONFIG.MAX_STEPS) > 0:
        max_iter = (
            min(CONFIG.MAX_STEPS, max_iter)
            if max_iter is not None
            else CONFIG.MAX_STEPS
        )
    assert max_iter is not None
    scheduler = cosine_scheduler(optimizer, max_iter, CONFIG.WARMUP_STEPS)
    return optimizer, scheduler


def train():
    model, processor = prepare_model_and_processor(["<yes/>", "<no/>"])

    train_dataloader, val_dataloader = prepare_dataloader(model, processor)

    # model.config.pad_token_id = processor.tokenizer.pad_token_id
    # model.config.decoder_start_token_id = processor.tokenizer.convert_tokens_to_ids(
    #     [""]
    # )[0]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)

    # Optimizer and Scheduler
    optimizer, scheduler = prepare_optimizer_and_scheduler(
        model, len(train_dataloader.dataset)
    )

    # Logger
    wandb.init(
        project="donut-funsd",
        name="docvqa",
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
    best_val_metric = 0.0
    steps = 0
    num_batches_per_epoch = len(train_dataloader)
    val_check_interval_batches = max(1, int(num_batches_per_epoch * CONFIG.VAL_CHECK_INTERVAL))

    for epoch in range(CONFIG.MAX_EPOCHS):
        # Training phase
        model.train()
        total_loss = 0
        for i, batch in enumerate(
            tqdm(train_dataloader, desc=f"Training Epoch {epoch+1}")
        ):
            pixel_values, decoder_input_ids, labels = batch
            pixel_values = pixel_values.to(device)
            decoder_input_ids = decoder_input_ids[:, :-1].to(device)
            labels = labels[:, 1:].to(device)

            with torch.autocast(device_type="cuda"):
                outputs = model(
                    pixel_values, decoder_input_ids=decoder_input_ids, labels=labels
                )
                loss = outputs.loss

            scaler.scale(loss).backward()

            if (i + 1) % CONFIG.ACCUMULATION_STEPS == 0:
                torch.nn.utils.clip_grad_norm_(
                    model.parameters(), CONFIG.GRADIENT_CLIP_VAL
                )
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()
                scheduler.step()

            # Log training metrics
            if steps % CONFIG.LOG_INTERVAL == 0:
                wandb.log(
                    {
                        "train/loss": loss.item(),
                        "gpu/memory_allocated": torch.cuda.memory_allocated(),
                        "gpu/memory_reserved": torch.cuda.memory_reserved(),
                        "lr": optimizer.param_groups[0]["lr"],
                    },
                    step=steps,
                )

            total_loss += loss.item()
            steps += 1

            if (i + 1) % val_check_interval_batches == 0:
                model.eval()
                torch.cuda.empty_cache()

                with torch.autocast(device_type="cuda"):
                    eval_results = evaluate_docvqa(
                        model=model,
                        processor=processor,
                        device=device,
                        val_dataloader=val_dataloader,
                        generation_config=GenerationConfig(
                            early_stopping=True,
                            num_beams=1,
                        ),
                    )

                wandb.log(
                    eval_results,
                    step=steps,
                )

                if best_val_metric < eval_results["eval/anls"]:
                    print("Saving Model!")
                    best_val_metric = eval_results["eval/anls"]
                    model.save_pretrained(model_dir)
                    processor.save_pretrained(processor_dir)

                torch.cuda.empty_cache()

        avg_train_loss = total_loss / len(train_dataloader)

        log_data = {"train/avg_loss": avg_train_loss}
        log_data.update({"epoch": epoch})

        wandb.log(
            log_data,
            step=steps,
        )

        torch.cuda.empty_cache()


if __name__ == "__main__":
    train()
