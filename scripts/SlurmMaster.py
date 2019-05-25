#!/usr/bin/env python

# Inspired by Nate Odell's (naodell@gmail.com) BatchMaster.py for condor
# https://github.com/NWUHEP/BLT/blob/topic_wbranch/BLTAnalysis/python/BatchMaster.py

import sys
import os
from pathlib import Path
import configparser
import json
import subprocess
import itertools
from datetime import datetime


class SlurmMaster:
    def __init__(self):
        self.date = datetime.today().strftime('%Y-%m-%d_%H-%M-%S')
        self.log_dir = Path('/pylon5/ac5616p/bpollack/mre_slurm', self.date)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.parse_config()

    def generate_slurm_script(self, number, conf):
        '''Make a slurm submission script.'''

        arg_string = ' '.join(f'--{i}={conf[i]}' for i in conf)
        script_name = f'/tmp/slurm_script_{self.date}_n{number}'
        script = open(script_name, 'w')
        script.write('#!/bin/bash\n')
        script.write('#SBATCH -A ac5616p\n')
        script.write('#SBATCH --partition=GPU-AI\n')
        script.write('#SBATCH --nodes=1\n')
        script.write('#SBATCH --gres=gpu:volta16:1\n')
        script.write('#SBATCH --time=1:00:00\n')
        script.write('#SBATCH --mail-user=brianleepollack@gmail.com\n')
        script.write(f'#SBATCH --output={str(self.log_dir)}/job_n{number}.stdout\n')
        script.write(f'#SBATCH --error={str(self.log_dir)}/job_n{number}.stderr\n')
        script.write('\n')

        script.write('set -x\n')
        script.write('echo "$@"\n')
        script.write('source /pghbio/dbmi/batmanlab/bpollack/anaconda3/etc/profile.d/conda.sh\n')
        script.write('conda activate new_mre\n')
        script.write('\n')

        script.write(f'python mre/train_model_full.py {arg_string}\n')

        script.close()
        return script_name

    def parse_config(self):
        config = configparser.ConfigParser()
        config.read('test_config.ini')
        section = config.sections()[0]
        self.config_dict = {}

        # Iterate through config and convert all scalars to lists
        for c in config[section]:
            val = json.loads(config[section][c])
            if type(val) == list:
                self.config_dict[c] = val
            else:
                self.config_dict[c] = [val]

        # Make every possible combo of config items
        self.config_combos = product_dict(**self.config_dict)

    def submit_scripts(self):
        for i, conf in enumerate(self.config_combos):
            script_name = self.generate_slurm_script(i, conf)
            print(script_name)
            subprocess.call(f'sbatch {script_name}', shell=True)


def product_dict(**kwargs):
    '''From https://stackoverflow.com/a/5228294/4942417,
    Produce all combos of configs for list-like items.'''
    keys = kwargs.keys()
    vals = kwargs.values()
    for instance in itertools.product(*vals):
        yield dict(zip(keys, instance))


if __name__ == "__main__":
    SM = SlurmMaster()
    SM.submit_scripts()
