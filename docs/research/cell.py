import os
import sys
import time

# ==========================================
# 1. NPU ENVIRONMENT & COMPILER BOOTSTRAP
# ==========================================
voe_path = os.path.join(sys.prefix, "Lib", "site-packages", "voe")
ort_capi_path = os.path.join(sys.prefix, "Lib", "site-packages", "onnxruntime", "capi")
host_compiler_path = r"C:\Program Files\RyzenAI\1.7.1\onnxruntime\bin"

for path in [voe_path, ort_capi_path, host_compiler_path]:
    if os.path.exists(path):
        os.add_dll_directory(path)

os.environ["PATH"] = (
    host_compiler_path
    + os.pathsep
    + voe_path
    + os.pathsep
    + ort_capi_path
    + os.pathsep
    + os.environ.get("PATH", "")
)

import onnxruntime as ort
import numpy as np

# ==========================================
# 2. PATHS & CACHE CONFIGURATION
# ==========================================
# Change this line in your script
model_path = r"F:\Omega3.0\runtime\bge_reranker\model_int8_fixed.onnx"
config_path = r"F:\Omega3.0\runtime\npu_config\vaip_config.json"
xclbin_path = r"F:\Omega3.0\runtime\npu_config\4x4.xclbin"
cache_path = r"F:\Omega3.0\runtime\npu_cache"

os.makedirs(cache_path, exist_ok=True)

# ==========================================
# 3. INITIALIZE VITIS AI SESSION (1.7.1 SPEC)
# ==========================================
providers = ["VitisAIExecutionProvider", "CPUExecutionProvider"]
provider_options = [
    {
        "config_file": config_path,
        "cacheDir": cache_path,
        "cacheKey": "bge_reranker_v1_7_1",
        "xclbin": xclbin_path,
        "target": "X1",  # <--- Change this from "Phoenix" to "X1"
    },
    {},
]
sess_options = ort.SessionOptions()
sess_options.log_severity_level = 3

try:
    print("Initializing ONNX session with VitisAI (NPU)...")
    print("The compiler is now finding its config. Initial load will take 1-2 minutes.")

    start_init = time.time()
    session = ort.InferenceSession(
        model_path,
        sess_options=sess_options,
        providers=providers,
        provider_options=provider_options,
    )
    end_init = time.time()

    print(f"\n[SUCCESS] NPU loaded in {end_init - start_init:.2f}s.")
    print("Providers actively used:", session.get_providers())
except Exception as e:
    print(f"\n[FAILED] NPU initialization failed. Error: {e}")
    sys.exit()

# ==========================================
# 4. DYNAMIC INPUTS & INFERENCE
# ==========================================
expected_inputs = session.get_inputs()
input_names = [i.name for i in expected_inputs]

dummy_input_ids = np.random.randint(100, 2000, size=(1, 128), dtype=np.int64)
dummy_mask = np.ones(shape=(1, 128), dtype=np.int64)

inputs = {input_names[0]: dummy_input_ids, input_names[1]: dummy_mask}
if len(input_names) > 2:
    inputs[input_names[2]] = np.zeros(shape=(1, 128), dtype=np.int64)
print("\n[STRESS TEST] Running 1000 iterations to spike Task Manager...")
start_test = time.time()
for _ in range(1000):
    session.run(None, inputs)
print(f"Done! 1000 iterations in {time.time() - start_test:.2f} seconds.")
