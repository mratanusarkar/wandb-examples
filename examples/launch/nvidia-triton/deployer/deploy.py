"""
Deploy a model artifact logged to W&B to Nvidia Triton
"""

import json
import os

import boto3
import click
import tritonclient.http as httpclient
from google.protobuf import json_format, text_format
from tritonclient.grpc import model_config_pb2

import wandb

# def config_pbtxt_to_dict(fname):
#     with open(fname) as f:
#         model_config = model_config_pb2.ModelConfig()
#         text_format.Parse(f.read(), model_config)
#         return json_format.MessageToDict(model_config)


def s3_config_pbtxt_to_dict(bucket, pbtxt_path):
    s3 = boto3.resource("s3")
    bucket = s3.Bucket(bucket)
    for obj in bucket.objects.all():
        # print(obj.key)
        if obj.key == pbtxt_path:
            model_config = model_config_pb2.ModelConfig()
            body = obj.get()["Body"]
            text_format.Parse(body.read(), model_config)
            return json_format.MessageToDict(model_config)


def dict_to_config_pbtxt(d, out_fname):
    with open(out_fname, "w") as f:
        model_config = model_config_pb2.ModelConfig()
        json_format.ParseDict(d, model_config)
        text_format.PrintMessage(model_config, f)
        wandb.termlog(f"Generated overloaded config at: {out_fname}")


def wandb_termlog_heading(text):
    return wandb.termlog(click.style(text, bg="white", bold=True))


def upload_files_to_triton_repo(artifact_path, remote_path, bucket_path):
    s3_client = boto3.client("s3")
    for root, _, files in os.walk(artifact_path):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, artifact_path)
            remote_obj_path = os.path.join(remote_path, rel_path)
            wandb.termlog(f"Uploading {rel_path} to {remote_obj_path}")
            s3_client.upload_file(full_path, bucket_path, remote_obj_path)


config = {
    "artifact": "wandb-artifact://megatruong/fashion-mnist-keras-triton/model-sage-feather-1:v2",
    "triton_url": "localhost:8000",
    "triton_bucket": "andrew-triton-bucket",
    "triton_model_repo_path": "models",
    "triton_model_config_overrides": {"max_batch_size": 32},
    "number_of_model_copies": 1,
}


with wandb.init(config=config, job_type="deploy_to_triton") as run:

    *_, model_name_ver = run.config.artifact.name.split("/")
    model_name, model_ver = model_name_ver.split(":v")
    model_ver = int(model_ver)
    if not isinstance(model_ver, int):
        raise ValueError("Triton requires model version to be an integer")

    if "triton_url" not in config:
        raise ValueError("`triton_url` must be specified in config")

    if "triton_bucket" not in config:
        raise ValueError(
            "`triton_bucket` must be specified in config in the form of your-bucket-name"
        )

    wandb_termlog_heading("Downloading wandb artifact")
    path = run.config.artifact.download()

    wandb_termlog_heading(
        "Uploading model to Triton model repo (this may take a while...)"
    )
    remote_path = (
        f"{run.config.triton_model_repo_path}/{model_name}/{model_ver}/model.savedmodel"
    )
    upload_files_to_triton_repo(path, remote_path, run.config.triton_bucket)

    wandb_termlog_heading("Loading model into Triton")
    with httpclient.InferenceServerClient(url=run.config.triton_url) as client:
        base_pbtxt_config = s3_config_pbtxt_to_dict(
            bucket=run.config.triton_bucket,
            pbtxt_path=f"{run.config.triton_model_repo_path}/{model_name}/config.pbtxt",
        )
        if not base_pbtxt_config:  # no config.pbtxt found
            wandb.termwarn(
                f"Did not find config.pbtxt for {model_name}:{model_ver}.  Trying to autogenerate config..."
            )
            try:
                client.load_model(model_name)  # try autogen
            except Exception as e:
                wandb.termwarn(
                    f"Unable to autogenerate config: {e}.  Continuing with empty base config."
                )
                base_pbtxt_config = {}
            else:
                wandb.termlog(
                    f"Using autogenerated config for {model_name}:{model_ver}"
                )
                base_pbtxt_config = client.get_model_config(model_name)
                client.unload_model(model_name)

        version_config = {"version_policy": {"specific": {"versions": [model_ver]}}}

        triton_configs = {
            **base_pbtxt_config,
            **version_config,
            **run.config.triton_model_config_overrides,
        }
        dict_to_config_pbtxt(triton_configs, "overloaded_config.pbtxt")
        client.load_model(model_name, config=json.dumps(triton_configs))

        if not client.is_model_ready(model_name):
            wandb.termerror(f"Failed to load model {model_name}")

    wandb_termlog_heading("Finished deploying to Triton")

    # Log code so it can be Launch'd
    run.log_code()
