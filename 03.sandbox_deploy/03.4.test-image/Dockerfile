# Test-only Jupyter image for the 2nd deployment (task01 / task11 / task21).
# It provides what the managed Hub config (50-workload template) expects:
#   jupyterhub + configurable-http-proxy, sudospawner, jupyterlab, sudo,
#   a "jupyterhub" group, and the config path /etc/jupyterhub/jupyterhub_config.py.
# Not a replacement for the real task image.
FROM quay.io/jupyterhub/jupyterhub:5.2.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends sudo \
 && rm -rf /var/lib/apt/lists/* \
 && PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --no-cache-dir jupyterlab==4.2.5 sudospawner==0.5.2 \
 && groupadd --force jupyterhub \
 && mkdir -p /etc/jupyterhub /home/jupyter

# The 50-workload Deployment mounts the rendered config here.
CMD ["jupyterhub", "-f", "/etc/jupyterhub/jupyterhub_config.py"]
