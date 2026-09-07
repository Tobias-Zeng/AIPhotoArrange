# encrypt_prompt.py
# ============================================================
# 开发者专用脚本：将 prompts/*.txt 明文提示词加密为 prompts/*.enc 密文文件
# ------------------------------------------------------------
# 用途：每次修改/新增提示词后，运行本脚本重新生成加密文件。
#   python encrypt_prompt.py
#
# 注意：本脚本仅用于开发期加密，不打包进发布版 exe。
#       运行时解密逻辑在 stage02_aesthetic_curator.py 的 _load_prompt_text() 中。
# ============================================================
import os
import glob
from cryptography.fernet import Fernet

# 固定密钥（与 stage02_aesthetic_curator.py 中 _STATIC_PROMPT_KEY 保持一致）
_STATIC_PROMPT_KEY = b"hfuLfxfmqclh5cbuVmVZVGbGmb-blZtvf42_YKV3_CU="

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def main():
    fernet = Fernet(_STATIC_PROMPT_KEY)
    txt_files = sorted(glob.glob(os.path.join(_PROMPTS_DIR, "*.txt")))
    if not txt_files:
        print(f"⚠️ 未在 {_PROMPTS_DIR} 找到任何 .txt 提示词文件")
        return

    print(f"找到 {len(txt_files)} 个明文提示词文件，开始加密...")
    count = 0
    for txt_path in txt_files:
        with open(txt_path, "rb") as f:
            plaintext = f.read()
        ciphertext = fernet.encrypt(plaintext)
        enc_path = os.path.splitext(txt_path)[0] + ".enc"
        with open(enc_path, "wb") as f:
            f.write(ciphertext)
        count += 1
        print(f"  [√] {os.path.basename(txt_path)} -> {os.path.basename(enc_path)} ({len(plaintext)}B -> {len(ciphertext)}B)")
    print(f"✅ 完成：共加密 {count} 个文件")


if __name__ == "__main__":
    main()