from typing import List, Optional, Tuple
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm
from transformers import (
    DonutProcessor,
    VisionEncoderDecoderModel,
)
from torch.nn.utils.rnn import pad_sequence
import config as CONFIG
from donut_distill.donut_dataset import DonutDataset
from donut_distill.metrics import calculate_metrics
from transformers import GenerationConfig
from donut_distill.other import postprocess_donut_funsd
import numpy as np

def evaluate(
    model: VisionEncoderDecoderModel,
    processor: DonutProcessor,
    device: torch.device,
    val_dataloader: DataLoader,
    generation_config: Optional[GenerationConfig] = None
    ):

    if generation_config == None:
        # Default generation config TODO:
        generation_config = GenerationConfig(early_stopping=True, num_beams=1)

    val_metrics = {
        "f1_score": [],
        "recall": [],
        "precision": []
    }

    model.eval()
    with torch.no_grad():
        for batch in tqdm(val_dataloader, desc="Validate"):
            pixel_values, decoder_input_ids, prompt_end_idxs, answers = batch
            pixel_values = pixel_values.to(device)

            decoder_prompts = pad_sequence(
                [
                    input_id[: end_idx + 1]
                    for input_id, end_idx in zip(
                        decoder_input_ids, prompt_end_idxs
                    )
                ],
                batch_first=True,
            ).to(device)

            outputs = model.generate(
                pixel_values,
                decoder_input_ids=decoder_prompts,
                max_length=CONFIG.MAX_LENGTH,
                pad_token_id=processor.tokenizer.pad_token_id,
                eos_token_id=processor.tokenizer.eos_token_id,
                use_cache=True,
                bad_words_ids=[[processor.tokenizer.unk_token_id]],
                return_dict_in_generate=True,
                generation_config=generation_config
            )

            predictions = processor.tokenizer.batch_decode(outputs.sequences)

            scores = []
            for pred, answer in zip(predictions, answers):
                answer = postprocess_donut_funsd(answer, processor)
                pred = postprocess_donut_funsd(pred, processor)

                f1_score, recall, precision = calculate_metrics(answer, pred)
                val_metrics["f1_score"].append(f1_score)
                val_metrics["recall"].append(recall)
                val_metrics["precision"].append(precision)

                if CONFIG.VERBOSE and len(scores) == 1:
                    print("\n----------------------------------------\n")
                    print(f"\nPrediction: {pred}")
                    print(f"\n\tAnswer: {answer}")
                    print(f"\n\tF1-Score: {f1_score}")

    val_metrics["f1_score"] = np.mean(val_metrics["f1_score"])
    val_metrics["recall"] = np.mean(val_metrics["recall"])
    val_metrics["precision"] = np.mean(val_metrics["precision"])

    return val_metrics


def test_generation_configs(model, processor, device, generationsconfigs: List[Tuple[str, GenerationConfig]]):
    val_dataset = DonutDataset(
        dataset_name_or_path="preprocessed_dataset",
        processor=processor,
        model=model,
        max_length=CONFIG.MAX_LENGTH,
        split="test",
        task_start_token="<s_funsd>",
        sort_json_key=False,  # cord dataset is preprocessed, so no need for this
    )

    val_dataloader = DataLoader(
        val_dataset,
        batch_size=1,
        shuffle=False,
        num_workers=CONFIG.NUM_WORKERS,
    )

    for description, generation_config in generationsconfigs:
        f1_score, recall, precision = evaluate(
            model=model,
            processor=processor,
            device = device,
            val_dataloader=val_dataloader,
            generation_config=generation_config,
        )

        print(100*'-')
        print(description)
        print("\tF1-score:", f1_score)


if __name__ == "__main__":
    generation_configs = [
        ("Top k - 50", GenerationConfig(
            do_sample=True,
            top_k=50
        )),
        ("Top k - 35", GenerationConfig(
            do_sample=True,
            top_k=35
        )),
        ("Top k - 20", GenerationConfig(
            do_sample=True,
            top_k=20
        )),
        ("Nucleus, p=0.9", GenerationConfig(
            do_sample=True,
            top_p=0.9,
            top_k=0
        )),
        ("Nucleus, p=0.95", GenerationConfig(
            do_sample=True,
            top_p=0.95,
            top_k=0
        )),
        ("Nucleus, p=0.92", GenerationConfig(
            do_sample=True,
            top_p=0.92,
            top_k=0
        )),
        ("Nucleus, p=0.94", GenerationConfig(
            do_sample=True,
            top_p=0.94,
            top_k=0
        )),
        ("Nucleus K, p=0.95 k=50", GenerationConfig(
            do_sample=True,
            top_k=50,
            top_p=0.95,
        )),
        ("Contrastive search, alpha=0.6, k=4", GenerationConfig(
            penalty_alpha=0.6, top_k=4,
        )),
        ("Contrastive search, alpha=0.8, k=4", GenerationConfig(
            penalty_alpha=0.8, top_k=4,
        )),
        ("Contrastive search, alpha=0.6, k=8", GenerationConfig(
            penalty_alpha=0.6, top_k=8,
        )),
        ("Contrastive search, alpha=0.6, k=10", GenerationConfig(
            penalty_alpha=0.6, top_k=10,
        )),
        ("Contrastive search, alpha=0.6, k=4", GenerationConfig(
            penalty_alpha=0.7, top_k=4,
        )),
        ("Nucleus K, p=0.95 k=40", GenerationConfig(
            do_sample=True,
            top_k=40,
            top_p=0.95,
        )),
        ("Nucleus K, p=0.94 k=50", GenerationConfig(
            do_sample=True,
            top_k=50,
            top_p=0.94,
        )),
        ("Nucleus K, p=0.93 k=40", GenerationConfig(
            do_sample=True,
            top_k=40,
            top_p=0.93,
        )),
        ("Nucleus K, p=0.92 k=30", GenerationConfig(
            do_sample=True,
            top_k=30,
            top_p=0.92,
        )),
        ("Greedy", GenerationConfig(
        )),
        ("Beam, num=5", GenerationConfig(
            num_beams=5,
            early_stopping=True
        )),
        ("Beam, num=3", GenerationConfig(
            num_beams=3,
            early_stopping=True
        )),
        ("Beam, num=7", GenerationConfig(
            num_beams=7,
            early_stopping=True
        )),
        ("Beam ngrams, num=5 ngrams=2", GenerationConfig(
            num_beams=5,
            no_repeat_ngram_size=2,
            early_stopping=True,
        )),
        ("Beam ngrams, num=5 ngrams=4", GenerationConfig(
            num_beams=5,
            no_repeat_ngram_size=4,
            early_stopping=True,
        )),
        ("Beam ngrams, num=5 ngrams=8", GenerationConfig(
            num_beams=5,
            no_repeat_ngram_size=8,
            early_stopping=True,
        )),
    ]
    import os

    donut_path = "result/donut_149"
    model_path = os.path.join(donut_path, "model")
    processor_path = os.path.join(donut_path, "processor")
    processor = DonutProcessor.from_pretrained(processor_path)
    model = VisionEncoderDecoderModel.from_pretrained(model_path)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)

    test_generation_configs(model, processor, device, generation_configs)