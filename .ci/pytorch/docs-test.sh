#!/bin/bash

# shellcheck source=./common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

echo "Testing pytorch docs"

sudo chown -R jenkins ../workspace

cd docs
whoami
pwd
ls -l
sudo mkdir source/scripts/activation_images
make doctest
