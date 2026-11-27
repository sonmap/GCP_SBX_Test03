FROM gcr.io/google.com/cloudsdktool/cloud-sdk:slim

ARG TERRAFORM_VERSION=1.9.8

COPY 03.sandbox_deploy/03.3.gke/requirements.txt /tmp/requirements.txt
COPY 03.sandbox_deploy/03.3.gke/run_terraform.py 03.sandbox_deploy/03.3.gke/reconcile_postdeploy.py /opt/sandbox/
COPY 03.sandbox_deploy/03.2.terraform/deployments/50-workload /opt/sandbox/terraform/50-workload

# Terraform and its providers are baked into the image: the private pool needs
# no registry access at run time, and the module version is tied to the image tag.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip kubectl python3-pip google-cloud-cli-gke-gcloud-auth-plugin \
    && pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && curl -fsSLo /tmp/terraform.zip "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
    && unzip -q /tmp/terraform.zip terraform -d /usr/local/bin \
    && terraform -chdir=/opt/sandbox/terraform/50-workload providers mirror /opt/sandbox/terraform/providers \
    && printf 'provider_installation {\n  filesystem_mirror {\n    path = "/opt/sandbox/terraform/providers"\n  }\n}\n' > /opt/sandbox/terraform/terraformrc \
    && chmod 0555 /opt/sandbox/run_terraform.py /opt/sandbox/reconcile_postdeploy.py \
    && rm -rf /var/lib/apt/lists/* /tmp/terraform.zip /tmp/requirements.txt

ENV TF_CLI_CONFIG_FILE=/opt/sandbox/terraform/terraformrc \
    TF_IN_AUTOMATION=1
