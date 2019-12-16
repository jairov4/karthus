import sys
import time
import json
import docker
import docker.errors
import boto3
import requests
import signal
import botocore.exceptions
from random import random


DOCKER_SOCK = '/var/run/docker.sock'
SELF_IMAGE = 'jairov4/swarm-sigterm-handler'


class S3Discovery:

    def __init__(self, bucket):
        self.client = boto3.client('s3')
        self.bucket = bucket

    def _list_objects(self, path):
        res = self.client.list_objects(Bucket=self.bucket, Prefix=path)
        if 'Contents' in res:
            return res['Contents']
        return []

    def _get_object(self, key):
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def _put_object(self, key, body):
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=body, ServerSideEncryption='AES256')

    def _object_exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except botocore.exceptions.ClientError as _e:
            return False

    def _delete_object(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def list_managers(self):
        while True:
            items = self._list_objects("managers")
            if len(items):
                log("Found %d managers, waiting 5 seconds before continuing..." % len(items))
                time.sleep(5)  # Give S3 time to syndicate all objects before next request
                return [json.loads(self._get_object(i['Key'])) for i in items]
            log("No managers found, waiting 5 seconds before retrying...")
            time.sleep(5)

    def add_manager(self, ip):
        data = {"ip": ip}
        self._put_object("managers/%s" % ip, json.dumps(data))

    def remove_manager(self, ip):
        self._delete_object("managers/%s" % ip)

    def get_tokens(self):
        return json.loads(self._get_object("tokens"))

    def get_token(self, role):
        tokens = self.get_tokens()
        return tokens[role]

    def set_tokens(self, data):
        self._put_object("tokens", json.dumps(data))

    def get_initial_lock(self, label="lock"):

        if self._object_exists("manager-init-lock"):
            return False

        log("Did not find existing swarm, attempting to initialize")

        lock_set = "%s: %f" % (label, random())

        self._put_object("manager-init-lock", lock_set)

        # Make sure we give other nodes time to check and write their IP
        # if our IP is still the one in the file after 5 seconds, then we are probably okay
        # to assume we are the manager
        time.sleep(5)

        lock_read = self._get_object("manager-init-lock")

        log("Comparing locks: %s => %s" % (lock_set, lock_read))

        return lock_read == lock_set


class SwarmHelper:

    def __init__(self, docker_client, node_ip):
        self.node_ip = node_ip
        self.docker_client = docker_client

    def is_in_swarm(self):
        return self.docker_client.info()["Swarm"]["LocalNodeState"] == "active"

    def init(self):
        self.docker_client.swarm.init(listen_addr=self.node_ip, advertise_addr=self.node_ip)

    def leave(self):
        self.docker_client.swarm.leave()

    def join_tokens(self):
        tokens = self.docker_client.swarm.attrs["JoinTokens"]
        return {"manager": tokens["Manager"], "worker": tokens["Worker"]}

    def join(self, token, managers):

        ips = [m["ip"] for m in managers]

        log("Attempting to join swarm as %s via managers %s" % (self.node_ip, ips))

        self.docker_client.swarm.join(
            remote_addrs=ips,
            join_token=token,
            listen_addr=self.node_ip,
            advertise_addr=self.node_ip
        )

        log("Joined swarm")


class GracefulKiller:

    def __init__(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
        self.kill_now = False

    def exit_gracefully(self, signum, _frame):
        log("Signal detected %s" % signum)
        self.kill_now = True


def log(l):
    sys.stdout.write(l + "\n")
    sys.stdout.flush()


class Ec2Metadata:

    @staticmethod
    def _read(ep):
        return requests.get('http://169.254.169.254/latest/meta-data/%s' % ep).text

    def __init__(self, ec2):
        self.instance_id = self._read('instance-id')
        self.az = self._read('placement/availability-zone/')
        self.region = self.az[:-1]
        self.instance = ec2.Instance(self.instance_id)
        self.role = [x['Value'] for x in self.instance.tags if x['Key'] == 'swarm-node-type'][0].lower()
        self.bucket = [x['Value'] for x in self.instance.tags if x['Key'] == 'swarm-state-bucket'][0]
        self.queue = [x['Value'] for x in self.instance.tags if x['Key'] == 'swarm-notification-queue'][0]
        # Propagation from ASG
        self.asg = [x['Value'] for x in self.instance.tags if x['Key'] == 'aws:cloudformation:logical-id'][0]
        self.stack_name = [x['Value'] for x in self.instance.tags if x['Key'] == 'aws:cloudformation:stack-name'][0]
        self.asg_name = [x['Value'] for x in self.instance.tags if x['Key'] == 'aws:autoscaling:groupName'][0]


class App:

    def __init__(self):
        self.sqs = boto3.resource('sqs')
        self.ec2 = boto3.resource('ec2')
        self.meta = Ec2Metadata(self.ec2)
        self.dockerc = docker.from_env()
        self.discovery = S3Discovery(self.meta.bucket)
        self.queue = self.sqs.Queue(self.meta.queue)
        self.swarm = SwarmHelper(self.dockerc, self.meta.instance.private_ip_address)
        log("Starting with %s" % self.meta.__dict__)

    def init(self):
        # os.system('ash initialize.sh')
        ip = self.meta.instance.private_ip_address
        if self.meta.role == 'manager' and self.discovery.get_initial_lock(ip):
            log("Initializing new swarm")
            self.swarm.init()
            self.discovery.set_tokens(self.swarm.join_tokens())

        else:
            log("Joining existing swarm")
            managers = self.discovery.list_managers()
            self.swarm.join(self.discovery.get_token(self.meta.role), managers)

        log("Tagging instance")
        self.meta.instance.create_tags(Tags=[
            dict(Key='swarm-node-id', Value=self.dockerc.info()['Swarm']['NodeID'])
        ])

        if self.meta.role == 'manager':
            self.meta.instance.create_tags(Tags=[
                dict(Key='swarm-cluster-id', Value=self.dockerc.swarm.id)
            ])

            log("Sending manager IP to discovery bucket")
            self.discovery.add_manager(ip)

            log("Running persistent container swarm_monitor")
            self.dockerc.containers.run(
                SELF_IMAGE,
                ['monitor'],
                detach=True,
                privileged=True,
                name='swarm_monitor',
                network_mode='host',
                restart_policy={"Name": "always"},
                environment={'AWS_DEFAULT_REGION': self.meta.region},
                volumes={DOCKER_SOCK: dict(bind=DOCKER_SOCK)})

    def monitor(self):
        gk = GracefulKiller()
        while not gk.kill_now:
            msgs = self.queue.receive_messages(WaitTimeSeconds=2)
            for msg in msgs:
                self.process_message(msg)

    def process_message(self, msg):
        log("Received notification")
        msg_body = json.loads(msg.body)
        transition = msg_body.get('LifecycleTransition')
        if transition == 'karthus:SWARM_NODE_LEAVE':
            node_id = msg_body.get('NodeId')
            log("Removing node: %s" % node_id)
            try:
                self.dockerc.api.remove_node(node_id, force=True)
            except docker.errors.NotFound:
                log("Node already removed %s" % node_id)

            msg.delete()
            return

        is_terminating = transition == 'autoscaling:EC2_INSTANCE_TERMINATING'
        instance_id = msg_body.get('EC2InstanceId')
        token = msg_body.get('LifecycleActionToken')
        hook_name = msg_body.get('LifecycleHookName')
        auto_scaling_group_name = msg_body.get('AutoScalingGroupName')
        if not all([instance_id, token, transition, hook_name, auto_scaling_group_name]):
            log('Deleting bad formed message: %s' % msg.body)
            msg.delete()
            return

        log("Notification: %s %s %s" % (instance_id, transition, token))
        instance = self.ec2.Instance(instance_id)
        try:
            instance.load()
            state = instance.state
        except Exception:
            log("Ignoring message about no longer existing instance")
            self.respond_lifecycle_hook(auto_scaling_group_name, hook_name, token)
            msg.delete()
            return

        if state['Name'] != 'running':
            log("Ignoring message about no longer running instance")
            self.respond_lifecycle_hook(auto_scaling_group_name, hook_name, token)
            msg.delete()
            return

        instance_ip = instance.private_ip_address
        if not is_terminating:
            log("Ignoring non terminating message: %s" % transition)
            self.respond_lifecycle_hook(auto_scaling_group_name, hook_name, token)
            msg.delete()
            return

        nodes = self.dockerc.nodes.list()
        node = next((x for x in nodes if x.attrs['Status']['Addr'] == instance_ip), None)
        if not node:
            log("Discarding message about node not found: %s" % instance_ip)
            self.respond_lifecycle_hook(auto_scaling_group_name, hook_name, token)
            msg.delete()
            return

        spec = node.attrs['Spec']
        is_manager = spec['Role'] == 'manager'
        if is_manager and self.meta.instance.id != instance_id:
            log("Node is a manager, must be attended by itself")
            msg.change_visibility(VisibilityTimeout=1)
            return

        msg.change_visibility(VisibilityTimeout=200)
        log("Removing node: %s" % node.attrs)
        if spec['Availability'] != 'drain':
            log("Set availability to drain")
            node.update({'Availability': 'drain', 'Role': spec['Role'], 'Labels': spec['Labels']})
            log("Waiting 50 seconds for draining")
            time.sleep(50)

        is_manager = node.attrs['Spec']['Role'] == 'manager'
        if is_manager:
            log("Demoting node manager")
            node.update({'Availability': 'drain', 'Role': 'worker', 'Labels': spec['Labels']})
            log("Waiting 15 seconds for demoting")
            time.sleep(15)
            log("Removing node from registry")
            self.discovery.remove_manager(instance_ip)
            self.dockerc.swarm.leave(force=True)

        msg.delete()
        self.respond_lifecycle_hook(auto_scaling_group_name, hook_name, token)
        data = dict(LifecycleTransition='karthus:SWARM_NODE_LEAVE', NodeId=node.id)
        self.queue.send_message(MessageBody=json.dumps(data))
        log("Successfully removed node")

    @staticmethod
    def respond_lifecycle_hook(auto_scaling_group_name, hook_name, token):
        asc = boto3.client('autoscaling')
        try:
            asc.complete_lifecycle_action(
                LifecycleHookName=hook_name,
                AutoScalingGroupName=auto_scaling_group_name,
                LifecycleActionToken=token,
                LifecycleActionResult='CONTINUE')
        except botocore.exceptions.ClientError as e:
            log("not so important but: %s" % e)


def main():
    app = App()
    action = sys.argv[1]
    if action == 'init':
        app.init()

    elif action == 'monitor':
        app.monitor()

    else:
        log("Invalid action: %s" % action)
        exit(1)


if __name__ == '__main__':
    main()
