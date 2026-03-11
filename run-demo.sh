#!/bin/bash

#
# *** Purpose ***
# Run a specific demo script for the project using the specified AWS SSO profile.
#
# *** Script Syntax ***
# ./run-demo.sh --profile=<SSO_PROFILE_NAME>
#               --mode=<MODE>
#               [--demo=<DEMO>]
#               [--run-id=<RUN_ID>]
#               [--save-schemas=<SAVE_DIR>]
#
#


set -euo pipefail  # Stop on error, undefined variables, and pipeline errors

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NO_COLOR='\033[0m'

# Function to print colored output
print_info() {
    echo -e "${GREEN}[INFO]${NO_COLOR} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NO_COLOR} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NO_COLOR} $1"
}

print_step() {
    echo -e "${BLUE}[STEP]${NO_COLOR} $1"
}

# Configuration folders
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/terraform"

print_info "Terraform Directory: $TERRAFORM_DIR"

argument_list="--profile=<SSO_PROFILE_NAME> --mode=<MODE>"


# Default required variables
AWS_PROFILE=""
mode=""
demo="all"
run_id=""
save_schemas_dir=""


# Get the arguments passed by shift to remove the first word
# then iterate over the rest of the arguments
for arg in "$@" # $@ sees arguments as separate words
do
    case $arg in
        *"--profile="*)
            AWS_PROFILE=$arg;;
        *"--mode="*)
            arg_length=7
            mode=${arg:$arg_length:$(expr ${#arg} - $arg_length)};;
        *"--demo="*)
            arg_length=7
            demo=${arg:$arg_length:$(expr ${#arg} - $arg_length)};;
        *"--run-id="*)
            arg_length=9
            run_id=${arg:$arg_length:$(expr ${#arg} - $arg_length)};;
        *"--save-schemas="*)
            arg_length=15
            save_schemas_dir=${arg:$arg_length:$(expr ${#arg} - $arg_length)};;
        *)
            echo
            print_error "(Error Message 001)  You included an invalid argument: $arg"
            echo
            print_error "Usage:  Require two argument ---> `basename $0` $argument_list"
            echo
            exit 85 # Common GNU/Linux Exit Code for 'Interrupted system call should be restarted'
            ;;
    esac
done

# Check required --profile argument was supplied
if [ -z "$AWS_PROFILE" ]
then
    echo
    print_error "(Error Message 002)  You did not include the proper use of the --profile=<SSO_PROFILE_NAME> argument in the call."
    echo
    print_error "Usage:  Require two argument ---> `basename $0` $argument_list"
    echo
    exit 85 # Common GNU/Linux Exit Code for 'Interrupted system call should be restarted'
fi

# Check required --mode argument was supplied
if [ -z "$mode" ]
then
    echo
    print_error "(Error Message 003)  You did not include the proper use of the --mode=<MODE> argument in the call."
    echo
    print_error "Usage:  Require two argument ---> `basename $0` $argument_list"
    echo
    exit 85 # Common GNU/Linux Exit Code for 'Interrupted system call should be restarted'
fi


# Get the AWS SSO credential variables that are used by the AWS CLI commands to authenicate
print_step "Authenticating to AWS SSO profile: $AWS_PROFILE..."
aws sso login $AWS_PROFILE
eval $(aws2-wrap $AWS_PROFILE --export)
export AWS_REGION=$(aws configure get region $AWS_PROFILE)

# Run the main.py script with the specified mode, demo, save-schemas directory, and/or run-id
if [ -z "$run_id" ]
then
    uv run python src/main.py --mode $mode --demo $demo --save-schemas $save_schemas_dir
else
    uv run python src/main.py --mode $mode --demo $demo --run-id $run_id --save-schemas $save_schemas_dir
fi
