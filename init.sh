#!/usr/bin/env bash
set -x
amazon-linux-extras install docker
usermod -a -G docker ec2-user
yum install -y awscli jq htop mc git

metadata() { curl -sSL "http://169.254.169.254/latest/meta-data/$1"; }
LOCAL_IP="$(metadata local-ipv4)"
INSTANCE_TYPE="$(metadata instance-type)"
INSTANCE_ID="$(metadata instance-id)"
NODE_AZ="$(metadata placement/availability-zone/)"
# shellcheck disable=SC2001
NODE_REGION="$(echo "$NODE_AZ" | sed 's/.$//')"
export AWS_DEFAULT_REGION="$NODE_REGION"

tag() { aws ec2 describe-tags --filters "Name=resource-id,Values=$INSTANCE_ID" "Name=key,Values=$1" | jq -r .Tags[0].Value; }
LOG_GROUP=$(tag LogGroup)
STACK_NAME=$(tag aws:cloudformation:stack-name)
ASG=$(tag aws:cloudformation:logical-id)

cat <<EOF > /etc/docker/daemon.json
{
  "experimental":false,
  "labels": [
    "os=linux", "region=$NODE_REGION", "az=$NODE_AZ", "instance_type=$INSTANCE_TYPE"
  ],
  "log-driver": "awslogs",
  "log-opts": {
    "awslogs-group": "$LOG_GROUP",
    "tag": "{{ with split .ImageName \":\" }}{{join . \"_\"}}{{end}}/$LOCAL_IP/{{.Name}}/{{.ID}}"
  }
}
EOF

modprobe br_netfilter && \
sysctl -w net.bridge.bridge-nf-call-iptables=1 && \
sysctl -w net.bridge.bridge-nf-call-ip6tables=1 && \
systemctl start docker && \
systemctl enable docker && \
docker plugin install --grant-all-permissions rexray/ebs && \
docker run \
  --privileged \
  --network host \
  --name aws-swarm-init \
  -e AWS_DEFAULT_REGION="$NODE_REGION" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  jairov4/swarm-sigterm-handler init

ERROR=$?

if [[ "$ERROR" == 0 ]]; then RESULT=SUCCESS; else RESULT=FAILURE; fi

echo "Setting status $RESULT"
aws cloudformation signal-resource \
  --stack "$STACK_NAME" \
  --logical-resource-id "$ASG" \
  --region "$NODE_REGION" \
  --unique-id "$INSTANCE_ID" \
  --status "$RESULT"

exit "$ERROR"
