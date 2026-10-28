#!/bin/zsh
cd -- "${0:A:h}"
python3 companion.py --configure-tailscale "$@" && python3 install_service.py
if [[ $? -ne 0 ]]; then
    read -r '?Press Return to close.'
fi
