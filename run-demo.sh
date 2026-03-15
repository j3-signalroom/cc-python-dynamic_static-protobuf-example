#!/bin/bash

#
# *** Purpose ***
# Run a specific demo script for the project using the specified AWS SSO profile.
#
# *** Script Syntax ***
# ./run-demo.sh --mode=<MODE>
#               --demo=<DEMO>
#               --schema-registry-url=<SCHEMA_REGISTRY_URL>
#               --sr-api-key=<SR_API_KEY>
#               --sr-api-secret=<SR_API_SECRET>
#               [--bootstrap-servers=<BOOTSTRAP_SERVERS>]
#               [--kafka-api-key=<KAFKA_API_KEY>]
#               [--kafka-api-secret=<KAFKA_API_SECRET>]
#               [--profile=<SSO_PROFILE_NAME>]
#               [--run-id=<RUN_ID>]
#               [--save-schemas=<SAVE_DIR>]
#               [--use-protoc]
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

argument_list="--mode=<MODE> --demo=<DEMO> --schema-registry-url=<SCHEMA_REGISTRY_URL> --sr-api-key=<SR_API_KEY> --sr-api-secret=<SR_API_SECRET>"


# Default required variables
AWS_PROFILE=""
mode=""
demo=""
run_id=""
save_schemas_dir=""
use_protoc=""
bootstrap_servers=""
kafka_api_key=""
kafka_api_secret=""
schema_registry_url=""
sr_api_key=""
sr_api_secret=""

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
        "--use-protoc")
            use_protoc="--use-protoc";;
        "--bootstrap-servers="*)
            arg_length=20
            bootstrap_servers=${arg:$arg_length:$(expr ${#arg} - $arg_length)};;
        "--kafka-api-key="*)
            arg_length=16
            kafka_api_key=${arg:$arg_length:$(expr ${#arg} - $arg_length)};;
        "--kafka-api-secret="*)
            arg_length=19
            kafka_api_secret=${arg:$arg_length:$(expr ${#arg} - $arg_length)};;
        "--schema-registry-url="*)
            arg_length=22
            schema_registry_url=${arg:$arg_length:$(expr ${#arg} - $arg_length)};;
        "--sr-api-key="*)
            arg_length=13
            sr_api_key=${arg:$arg_length:$(expr ${#arg} - $arg_length)};;
        "--sr-api-secret="*)
            arg_length=16
            sr_api_secret=${arg:$arg_length:$(expr ${#arg} - $arg_length)};;
        *)
            echo
            print_error "(Error Message 001)  You included an invalid argument: $arg"
            echo
            print_error "Usage:  Require five argument ---> `basename $0` $argument_list"
            echo
            exit 85 # Common GNU/Linux Exit Code for 'Interrupted system call should be restarted'
            ;;
    esac
done

# Check required --mode argument was supplied
if [ -z "$mode" ]
then
    echo
    print_error "(Error Message 002)  You did not include the proper use of the --mode=<MODE> argument in the call."
    echo
    print_error "Usage:  Require five argument ---> `basename $0` $argument_list"
    echo
    exit 85 # Common GNU/Linux Exit Code for 'Interrupted system call should be restarted'
fi

# Check required --demo argument was supplied
if [ -z "$demo" ]
then
    echo
    print_error "(Error Message 006)  You did not include the proper use of the --demo=<DEMO> argument in the call."
    echo
    print_error "Usage:  Require five argument ---> `basename $0` $argument_list"
    echo
    exit 85 # Common GNU/Linux Exit Code for 'Interrupted system call should be restarted'
fi

# Check required --schema-registry-url argument was supplied
if [ -z "$schema_registry_url" ]
then
    echo
    print_error "(Error Message 003)  You did not include the proper use of the --schema-registry-url=<SCHEMA_REGISTRY_URL> argument in the call."
    echo
    print_error "Usage:  Require five argument ---> `basename $0` $argument_list"
    echo
    exit 85 # Common GNU/Linux Exit Code for 'Interrupted system call should be restarted'
fi

# Check required --sr-api-key argument was supplied
if [ -z "$sr_api_key" ]
then
    echo
    print_error "(Error Message 004)  You did not include the proper use of the --sr-api-key=<SR_API_KEY> argument in the call."
    echo
    print_error "Usage:  Require five argument ---> `basename $0` $argument_list"
    echo
    exit 85 # Common GNU/Linux Exit Code for 'Interrupted system call should be restarted'
fi

# Check required --sr-api-secret argument was supplied
if [ -z "$sr_api_secret" ]
then            
    echo
    print_error "(Error Message 005)  You did not include the proper use of the --sr-api-secret=<SR_API_SECRET> argument in the call."
    echo
    print_error "Usage:  Require five argument ---> `basename $0` $argument_list"
    echo
    exit 85 # Common GNU/Linux Exit Code for 'Interrupted system call should be restarted'
fi

# Check required AWS_PROFILE environment variable was supplied
if [ "$demo" = "csfle" ] || [ "$demo" = "all" ]; then
    if [ -z "$AWS_PROFILE" ]
    then            
        echo
        print_error "(Error Message 007)  You did not include the proper use of the AWS_PROFILE environment variable in the call."
        echo
        print_error "Usage:  Require five argument ---> `basename $0` $argument_list"
        echo
        exit 85 # Common GNU/Linux Exit Code for 'Interrupted system call should be restarted'
    fi
fi

if [ -n "$AWS_PROFILE" ]
then
    # Get the AWS SSO credential variables that are used by the AWS CLI commands to authenticate
    print_step "Authenticating to AWS SSO profile: $AWS_PROFILE..."
    aws sso login $AWS_PROFILE
    eval $(aws2-wrap $AWS_PROFILE --export)
    export AWS_REGION=$(aws configure get region $AWS_PROFILE)

    # Get the current AWS account ID and caller ARN for the key policy
    export AWS_ACCOUNT_ID=$(aws sts get-caller-identity $AWS_PROFILE --query "Account" --output text)
    AWS_CALLER_ARN=$(aws sts get-caller-identity --query "Arn" --output text)
    print_info "AWS Account ID: $AWS_ACCOUNT_ID"
    print_info "AWS Caller ARN: $AWS_CALLER_ARN"
fi

# ── Provision the KMS KEK when CSFLE demo is selected ─────────────────────
if [ "$demo" = "csfle" ] || [ "$demo" = "all" ]; then
    # Clean up any existing KMS resources from previous runs to ensure a clean slate for the demo
    aws kms delete-alias --alias-name alias/confluent-csfle-kek --region "$AWS_REGION" 2>/dev/null \
        || print_warn "KMS alias not found, skipping deletion"

    print_step "Provisioning KMS KEK via AWS CLI..."

    # Build the KMS key policy with permissions for the current AWS account root and the caller ARN
    KMS_KEY_POLICY=$(cat <<POLICY
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowRootAccountFullAccess",
            "Effect": "Allow",
            "Principal": {
                "AWS": "arn:aws:iam::${AWS_ACCOUNT_ID}:root"
            },
            "Action": "kms:*",
            "Resource": "*"
        },
        {
            "Sid": "AllowCSFLEOperations",
            "Effect": "Allow",
            "Principal": {
                "AWS": "${AWS_CALLER_ARN}"
            },
            "Action": [
                "kms:Encrypt",
                "kms:Decrypt",
                "kms:GenerateDataKey",
                "kms:DescribeKey"
            ],
            "Resource": "*"
        }
    ]
}
POLICY
    )

    # Create the KMS key
    print_info "Creating KMS key..."
    KMS_KEY_ID=$(aws kms create-key \
        --description "KEK (Key Encryption Key) for Confluent CSFLE demo" \
        --policy "$KMS_KEY_POLICY" \
        --tags TagKey=Purpose,TagValue=confluent-csfle-kek TagKey=ManagedBy,TagValue=aws-cli \
        --region "$AWS_REGION" \
        --query "KeyMetadata.KeyId" \
        --output text)
    print_info "KMS Key ID: $KMS_KEY_ID"

    # Enable automatic key rotation
    print_info "Enabling automatic key rotation..."
    aws kms enable-key-rotation --key-id "$KMS_KEY_ID" --region "$AWS_REGION"

    # Create the KMS alias
    print_info "Creating KMS alias..."
    aws kms create-alias \
        --alias-name alias/confluent-csfle-kek \
        --target-key-id "$KMS_KEY_ID" \
        --region "$AWS_REGION"

    # Export the KMS key ARN for the CSFLE demo
    export AWS_KMS_KEY_ARN=$(aws kms describe-key \
        --key-id "$KMS_KEY_ID" \
        --region "$AWS_REGION" \
        --query "KeyMetadata.Arn" \
        --output text)
    print_info "AWS_KMS_KEY_ARN=${AWS_KMS_KEY_ARN}"
    print_info "KMS KEK provisioned successfully!"
fi

# Create a .env file with the necessary environment variables for the demo script
printf "BOOTSTRAP_SERVERS=\"${bootstrap_servers}\"\
\nKAFKA_API_KEY=\"${kafka_api_key}\"\
\nKAFKA_API_SECRET=\"${kafka_api_secret}\"\
\nSCHEMA_REGISTRY_URL=\"${schema_registry_url}\"\
\nSR_API_KEY=\"${sr_api_key}\"\
\nSR_API_SECRET=\"${sr_api_secret}\"" > .env

# Build the argument list for the demo script
cmd_args="--mode $mode --demo $demo"
[ -n "$run_id" ] && cmd_args="$cmd_args --run-id $run_id"
[ -n "$save_schemas_dir" ] && cmd_args="$cmd_args --save-schemas $save_schemas_dir"
[ -n "$use_protoc" ] && cmd_args="$cmd_args --use-protoc"

# Run the demo script
uv run python src/main.py $cmd_args
