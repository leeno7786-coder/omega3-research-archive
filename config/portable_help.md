# Omega 3.0 quick guide

Omega is a local-first cognitive runtime. The setup wizard chooses the model seat
that supplies the mind; the voice and memory services remain part of the portable
Omega product tree.

## The two model seats

- **Local LM Studio** keeps model traffic on the computer. Start the portable LM
  Studio GUI from this wizard, load a model, enable its OpenAI-compatible server,
  and enter the model id shown by LM Studio. Its model downloads are kept in the
  `models` folder beside the LM Studio launcher on the portable drive.
- **Cloud provider** connects Omega to an OpenAI-compatible cloud endpoint. Choose
  a provider, enter its API key, test the connection, and select the model id that
  the provider exposes. The key is encrypted on the product drive and is never
  written to the setup JSON or displayed after saving.

## Optional Linux runtime

The Windows setup wizard includes **Launch Linux runtime** for an SSD that also
contains the Linux payload. Keep the Linux bundle in the sibling
`LM-Studio-Linux/run-ai.sh` layout on the portable drive, install WSL, and press
the button. Omega starts
a hidden background WSL process in that folder and runs:

```text
nohup ./run-ai.sh > ./lm-studio.log 2>&1 &
```

The Linux payload is an optional sibling runtime on the same portable drive. The
Windows Omega product does not copy or modify that bundle; it only starts its
portable script through WSL when the operator selects the Linux runtime.

## Browser automation

Browser tasks use the installed Microsoft Edge browser and the EdgeDriver archive
bundled with Omega. Their first three version numbers must match. Omega checks
the versions and the archive checksum before starting the browser; it does not
download a replacement driver. If Edge has updated beyond the bundled driver,
browser tasks need a package with a matching EdgeDriver release.

## Vision

Omega vision has two simple parts:

- **Face memory** runs locally on the CPU. The face model files must be present
  in `models/onnxs/face/`. Omega uses these files to map faces, remember names,
  and recognize them again later. Face vision does not use the DME NPU.
- **Scene understanding** uses the selected mind seat. For offline use, load a
  vision-capable multimodal model in LM Studio and enable its OpenAI-compatible
  server. For cloud use, choose a provider and a model that accepts image input.

To use vision:

1. Keep the `models` folder beside the Omega product files.
2. For offline vision, load a model that can read images in LM Studio.
3. For cloud vision, select a vision-capable model and complete the provider setup.
4. Use **attach** in the chat bar to upload an image, or use **face memory** in
   the operator profile to save a named face.
5. Allow file or camera access when Windows or the browser asks.

If text chat works but an image is not described, the selected model probably
does not support image input. Face mapping can still work through the local CPU
face models as long as `models/onnxs/face/` is present.

## The voice and DME path

Parakeet handles speech recognition, Qwen3-TTS handles speech synthesis, and the
portable DME runtime supports memory recall. Omega starts these services from the
product drive when you press **Save & Boot Omega**.

On Windows, DME tries the bundled VitisAI NPU provider first and requires a
proven NPU placement before it reports `npu`; otherwise it reports and uses the
CPU route. The NPU is never used for speech. Parakeet runs on CPU for either
mind seat. Qwen3-TTS runs on CPU when LM Studio is selected and on the configured
Vulkan GPU when a cloud mind is selected. Both speech models are warmed before
the local-services gateway reports ready and stay loaded until shutdown. A
cloud mind seat fails closed if Qwen cannot use its configured Vulkan backend.
Windows needs an installed AMD graphics driver with Vulkan support for cloud
speech synthesis, and a compatible Ryzen AI/XDNA driver for DME NPU access. The
product drive carries the user-mode payloads, not OS kernel drivers.

## Shutdown

The HUD shutdown button releases the in-process semantic retina and DME seats,
closes DME, Qwen3-TTS, Parakeet, and face-model sessions, and stops the Omega
local-services process. It also stops the portable Windows LM Studio process
tree and, when WSL is available, requests the sibling Linux LM Studio bundle to
stop. Only processes proven to belong to this portable SSD bundle are targeted;
unrelated host AI applications are left alone.

## What happens after setup

The wizard saves the selected backend and model under the hidden product tree. On
the next launch it reloads that state instead of starting Omega immediately. You
can change the provider, endpoint, API key, or model, save for later, or press
**Save & Boot Omega** when the configuration is ready.

## Privacy

Local model traffic stays local. Cloud traffic follows the privacy policy of the
provider you select and the Omega policy shown in the setup wizard. Omega does not
silently switch between local and cloud seats.
