#!/bin/zsh
cd -- "${0:A:h}"
python3 companion.py "$@"
if [[ $? -ne 0 ]]; then
    read -r '?Press Return to close.'
fi
