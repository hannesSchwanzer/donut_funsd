from transformers import VisionEncoderDecoderModel
import config as CONFIG

# Load the pretrained LayoutLMv3 model
model = VisionEncoderDecoderModel.from_pretrained(CONFIG.MODEL_ID)

# Function to calculate parameter sizes
def analyze_parameters(model: VisionEncoderDecoderModel, finegrained: bool = False):
    total_params = 0
    module_params = {}

    # Iterate over named parameters
    for name, param in model.named_parameters():
        param_count = param.numel() # Get the number of elements in the tensor
        total_params += param_count
        print(f"{name}: {param_count}")

        # Group parameters by module (e.g., "encoder.layer.0", "embeddings")
        module_name = name.split('.')
        if finegrained:
            try:
                module_name = module_name[0] + "_layer_" + module_name[module_name.index("layers")+1]
            except ValueError:
                module_name = module_name[0]
        else:
            module_name = module_name[0]

        if module_name not in module_params:
            module_params[module_name] = 0
        module_params[module_name] += param_count

    # Sort modules by size
    sorted_modules = sorted(module_params.items(), key=lambda x: x[1], reverse=True)

    # Print parameter statistics
    print(f"Total Parameters: {total_params:,}")
    print("Parameter count by module:")
    for module, count in sorted_modules:
        print(f"{module}: {count:,} ({count / total_params:.2%})")

# Analyze the model parameters
analyze_parameters(model, True)
