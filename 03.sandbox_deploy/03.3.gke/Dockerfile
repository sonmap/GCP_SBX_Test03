FROM gcr.io/google.com/cloudsdktool/cloud-sdk:slim

# ALB step only. Terraform runs in Infrastructure Manager, not in this image.
COPY 03.sandbox_deploy/03.3.gke/requirements.txt /tmp/requirements.txt
COPY 03.sandbox_deploy/03.3.gke/reconcile_postdeploy.py /opt/sandbox/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates kubectl python3-pip google-cloud-cli-gke-gcloud-auth-plugin \
    && pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && chmod 0555 /opt/sandbox/reconcile_postdeploy.py \
    && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt
