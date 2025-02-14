import json
import random
from typing import Any, List
import torch
from torch.utils.data import Dataset
from datasets import load_dataset
from transformers import DonutProcessor, VisionEncoderDecoderModel

# https://github.com/NielsRogge/Transformers-Tutorials/blob/master/Donut/CORD/Fine_tune_Donut_on_a_custom_dataset_(CORD)_with_PyTorch_Lightning.ipynb
added_tokens = []
pad_token_id = "0"

# def collate_fn_docvqa_eval(batch):
#     if len(batch[0]) == 4:
#         pixel_values, input_ids, prompt_end_idxs, target_sequences = zip(*batch)
#
#         # Stack pixel_values and input_ids into tensors
#         pixel_values = torch.stack(pixel_values)
#         input_ids = torch.stack(input_ids)
#         prompt_end_idxs = torch.tensor(prompt_end_idxs)
#
#         # Ensure target_sequences are always lists (for ANLS computation)
#         processed_targets = []
#         for target in target_sequences:
#             if isinstance(target, str):
#                 processed_targets.append([target])  # Wrap single GT in a list
#             else:
#                 processed_targets.append(target)  # Already a list of multiple GTs
#
#         return pixel_values, input_ids, prompt_end_idxs, processed_targets
#     else: 
#         raise Exception()


class DonutDataset(Dataset):
    """
    PyTorch Dataset for Donut. This class takes a HuggingFace Dataset as input.

    Each row, consists of image path(png/jpg/jpeg) and gt data (json/jsonl/txt),
    and it will be converted into pixel_values (vectorized image) and labels (input_ids of the tokenized string).

    Args:
        dataset_name_or_path: name of dataset (available at huggingface.co/datasets) or the path containing image files and metadata.jsonl
        max_length: the max number of tokens for the target sequences
        split: whether to load "train", "validation" or "test" split
        ignore_id: ignore_index for torch.nn.CrossEntropyLoss
        task_start_token: the special token to be fed to the decoder to conduct the target task
        prompt_end_token: the special token at the end of the sequences
        sort_json_key: whether or not to sort the JSON keys
    """

    def __init__(
        self,
        processor: DonutProcessor,
        model: VisionEncoderDecoderModel,
        dataset_name_or_path: str,
        max_length: int,
        split: str = "train",
        ignore_id: int = -100,
        task_start_token: str = "<s>",
        prompt_end_token: str = None,
        sort_json_key: bool = True,
        task: str = "",
    ):
        super().__init__()

        self.processor = processor
        self.model = model
        self.max_length = max_length
        self.split = split
        self.ignore_id = ignore_id
        self.task_start_token = task_start_token
        self.prompt_end_token = (
            prompt_end_token if prompt_end_token else task_start_token
        )
        self.sort_json_key = sort_json_key
        self.task = task

        self.dataset = load_dataset(dataset_name_or_path, split=self.split)
        self.dataset_length = len(self.dataset)

        self.gt_token_sequences = []
        for sample in self.dataset:
            ground_truth = json.loads(sample["ground_truth"])
            if (
                "gt_parses" in ground_truth
            ):  # when multiple ground truths are available, e.g., docvqa
                assert isinstance(ground_truth["gt_parses"], list)
                gt_jsons = ground_truth["gt_parses"]
            else:
                assert "gt_parse" in ground_truth and isinstance(
                    ground_truth["gt_parse"], dict
                )
                gt_jsons = [ground_truth["gt_parse"]]

            self.gt_token_sequences.append(
                [
                    self.json2token(
                        gt_json,
                        update_special_tokens_for_json_key=self.split == "train",
                        sort_json_key=self.sort_json_key,
                    )
                    + self.processor.tokenizer.eos_token
                    for gt_json in gt_jsons  # load json from list of json
                ]
            )

        self.add_tokens([self.task_start_token, self.prompt_end_token])
        self.prompt_end_token_id = self.processor.tokenizer.convert_tokens_to_ids(
            self.prompt_end_token
        )


    def json2token(
        self,
        obj: Any,
        update_special_tokens_for_json_key: bool = True,
        sort_json_key: bool = True,
    ):
        """
        Convert an ordered JSON object into a token sequence
        """
        if type(obj) is dict:
            if len(obj) == 1 and "text_sequence" in obj:
                return obj["text_sequence"]
            else:
                output = ""
                if sort_json_key:
                    keys = sorted(obj.keys(), reverse=True)
                else:
                    keys = obj.keys()
                for k in keys:
                    if update_special_tokens_for_json_key:
                        self.add_tokens([rf"<s_{k}>", rf"</s_{k}>"])
                    output += (
                        rf"<s_{k}>"
                        + self.json2token(
                            obj[k], update_special_tokens_for_json_key, sort_json_key
                        )
                        + rf"</s_{k}>"
                    )
                return output
        elif type(obj) is list:
            return r"<sep/>".join(
                [
                    self.json2token(
                        item, update_special_tokens_for_json_key, sort_json_key
                    )
                    for item in obj
                ]
            )
        else:
            obj = str(obj)
            if f"<{obj}/>" in added_tokens:
                obj = f"<{obj}/>"  # for categorical special tokens
            return obj

    def add_tokens(self, list_of_tokens: List[str]):
        """
        Add special tokens to tokenizer and resize the token embeddings of the decoder
        """
        newly_added_num = self.processor.tokenizer.add_tokens(list_of_tokens)
        if newly_added_num > 0:
            self.model.decoder.resize_token_embeddings(len(self.processor.tokenizer))
            self.model.config.vocab_size = len(self.processor.tokenizer)
            added_tokens.extend(list_of_tokens)

    def __len__(self) -> int:
        return self.dataset_length

    def __getitem__(self, idx: int):
        """
        Load image from image_path of given dataset_path and convert into input_tensor and labels
        Convert gt data into input_ids (tokenized string)
        Returns:
            input_tensor : preprocessed image
            input_ids : tokenized gt_data
            labels : masked labels (model doesn't need to predict prompt and pad token)
        """
        sample = self.dataset[idx]

        # inputs
        image = sample["image"].convert("RGB")
        pixel_values = self.processor(
            # image, random_padding=self.split == "train", return_tensors="pt"
            image, random_padding=self.split == "train", return_tensors="pt"
        ).pixel_values
        pixel_values = pixel_values.squeeze()

        # targets
        target_sequence = random.choice(
            self.gt_token_sequences[idx]
        )  # can be more than one, e.g., DocVQA Task 1
        input_ids = self.processor.tokenizer(
            target_sequence,
            add_special_tokens=False,
            max_length=self.max_length,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )["input_ids"].squeeze(0)

        if self.split == "train":
            labels = input_ids.clone()
            labels[labels == self.processor.tokenizer.pad_token_id] = self.ignore_id # model doesn't need to predict pad token
            labels[: torch.nonzero(labels == self.prompt_end_token_id).sum() + 1] = self.ignore_id  # model doesn't need to predict prompt (for VQA)
            return pixel_values, input_ids, labels
        else:
            prompt_end_index = torch.nonzero(
                input_ids == self.prompt_end_token_id
            ).sum()  # return prompt end index instead of target output labels
            if self.task == 'docvqa':
                return pixel_values, input_ids, prompt_end_index, "\n".join(self.gt_token_sequences[idx])
            else:
                return pixel_values, input_ids, prompt_end_index, target_sequence
