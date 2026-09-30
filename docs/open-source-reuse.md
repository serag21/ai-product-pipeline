# Open-source reuse

This project intentionally reuses established ComfyUI integration patterns instead of rebuilding the execution protocol.

The initial ComfyUI client in `src/comfy/client.py` is adapted from the client in the public OpenMontage repository:

https://github.com/calesthio/OpenMontage/blob/main/tools/_comfyui/client.py

OpenMontage is licensed under the GNU Affero General Public License v3. The repository includes the AGPLv3 license text and the adapted file carries a source attribution notice.

The generated coloring-book assets are separate outputs of this software pipeline; the software license applies to the software, not merely because the software generated an image.
