''' General '''
DISTILL = False
RESULT_PATH= "./result/docvqa"
VERBOSE= True
LOG_INTERVAL = 10 # After how many steps the logger should log

''' transformer parameters '''
MODEL_ID            = 'naver-clova-ix/donut-base'
MAX_LENGTH          = 128
INPUT_SIZE= [1280, 960] # when the input resolution differs from the pre-training setting, some weights will be newly initialized (but the model training would be okay)

''' Dataset parameters '''
DATASET= "./preprocessed_dataset_docvqa/" # loading datasets (from moldehub or path)
DATASET_NAME_TRAINING="train"
DATASET_NAME_VALIDATE="validation"
SORT_JSON_KEY= False
ALIGN_LONG_AXIS= False

''' Train parameters '''
TRAIN_BATCH_SIZES=4
ACCUMULATION_STEPS = 1
LR= 3e-5
GRADIENT_CLIP_VAL= 0.25

NUM_NODES= 1
NUM_WORKERS= 0

WARMUP_STEPS= 10000 # 800/8*30/10, 10%
MAX_EPOCHS= 30
MAX_STEPS= -1

''' Validation parameters '''
VAL_BATCH_SIZES=1
VAL_CHECK_INTERVAL = 0.2
LIMIT_VAL_BATCHES = 1

''' Distillation parameters '''
TEACHER_MODEL_PATH = 'result/docvqa/best_model'
DECODER_LAYER_MAP = [1, 3, 4]
ENCODER_LAYER_MAP = []

