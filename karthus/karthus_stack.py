from aws_cdk import core, aws_ec2 as ec2, aws_autoscaling as autoscaling, aws_logs as logs, aws_iam as iam, aws_s3 as s3
from aws_cdk import aws_autoscaling_hooktargets as as_ht, aws_sqs as sqs


class KarthusStack(core.Stack):

    def __init__(self, scope: core.Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        cidr = '10.10.10.0/24'
        ssh_key = 'katarina-test'

        vpc = ec2.Vpc(self, "Cluster", cidr=cidr, max_azs=3, enable_dns_support=True, enable_dns_hostnames=True)
        log_group = logs.LogGroup(self, "LogGroup", retention=logs.RetentionDays.THREE_DAYS)

        script = open('init.sh', 'rb').read()
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(str(script, 'utf-8'))

        swarm_bucket = s3.Bucket(self, "SwarmBucket")
        sg = ec2.SecurityGroup(self, "Swarm", description='Swarm nodes', vpc=vpc)
        sg.add_ingress_rule(sg, ec2.Port.icmp_ping(), description='ICMP ping')
        sg.add_ingress_rule(sg, ec2.Port.tcp(2377), description='Swarm management')
        sg.add_ingress_rule(sg, ec2.Port.tcp(7946), description='Swarm Communication')
        sg.add_ingress_rule(sg, ec2.Port.udp(7946), description='Swarm Communication')
        sg.add_ingress_rule(sg, ec2.Port.udp(4789), description='Swarm Overlay Network')
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(22), description='SSH')
        sg.add_ingress_rule(ec2.Peer.any_ipv6(), ec2.Port.tcp(22), description='SSH')

        queue = sqs.Queue(self, "SwarmNodeNotifications")

        instance_policy = iam.PolicyDocument(statements=[
            # S3 Swarm Bucket
            iam.PolicyStatement(
                actions=['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListObjects'],
                resources=[swarm_bucket.arn_for_objects('*')]),
            iam.PolicyStatement(
                actions=['s3:ListBucket'],
                resources=[swarm_bucket.bucket_arn]),
            # CloudWatch cluster logs
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams", "logs:GetLogEvents"],
                resources=[log_group.log_group_arn]),
            # Auto discovery capabilities
            iam.PolicyStatement(
                actions=["ec2:DescribeInstances", "ec2:DescribeTags", "ec2:CreateTags"],
                resources=['*']),
            # CloudFormation instance signaling
            iam.PolicyStatement(
                actions=["cloudformation:SignalResource"],
                resources=["*"]),
            # Swarm Node notifications
            iam.PolicyStatement(
                actions=[
                    "sqs:ChangeMessageVisibility",
                    "sqs:ChangeMessageVisibilityBatch",
                    "sqs:DeleteMessage",
                    "sqs:DeleteMessageBatch",
                    "sqs:ReceiveMessage",
                    "sqs:SendMessage"
                ],
                resources=[queue.queue_arn]),
            # LifeCycle hooks response
            iam.PolicyStatement(
                actions=['autoscaling:CompleteLifecycleAction'],
                resources=['*']),
            # RexRay EBS
            iam.PolicyStatement(
                actions=[
                    "ec2:AttachVolume",
                    "ec2:CreateVolume",
                    "ec2:CreateSnapshot",
                    "ec2:CreateTags",
                    "ec2:DeleteVolume",
                    "ec2:DeleteSnapshot",
                    "ec2:DescribeAvailabilityZones",
                    "ec2:DescribeInstances",
                    "ec2:DescribeVolumes",
                    "ec2:DescribeVolumeAttribute",
                    "ec2:DescribeVolumeStatus",
                    "ec2:DescribeSnapshots",
                    "ec2:CopySnapshot",
                    "ec2:DescribeSnapshotAttribute",
                    "ec2:DetachVolume",
                    "ec2:ModifySnapshotAttribute",
                    "ec2:ModifyVolumeAttribute",
                    "ec2:DescribeTags"
                ],
                resources=["*"])
        ])

        role = iam.Role(
            self, "InstanceRole", path='/', assumed_by=iam.ServicePrincipal('ec2.amazonaws.com'),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name('AmazonSSMManagedInstanceCore')],
            inline_policies={'Policy': instance_policy})

        rolling_update_config = autoscaling.RollingUpdateConfiguration(
            min_instances_in_service=1, max_batch_size=1, pause_time=core.Duration.minutes(5),
            wait_on_resource_signals=True, suspend_processes=[
                autoscaling.ScalingProcess.HEALTH_CHECK,
                autoscaling.ScalingProcess.REPLACE_UNHEALTHY,
                autoscaling.ScalingProcess.AZ_REBALANCE,
                autoscaling.ScalingProcess.ALARM_NOTIFICATION,
                autoscaling.ScalingProcess.SCHEDULED_ACTIONS
            ])

        asg_managers = autoscaling.AutoScalingGroup(
            self,
            "Managers",
            vpc=vpc,
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3_AMD, ec2.InstanceSize.NANO),
            machine_image=ec2.AmazonLinuxImage(generation=ec2.AmazonLinuxGeneration.AMAZON_LINUX_2),
            key_name=ssh_key,
            user_data=user_data,
            min_capacity=3,
            vpc_subnets=ec2.SubnetSelection(subnets=vpc.public_subnets),
            role=role,
            update_type=autoscaling.UpdateType.ROLLING_UPDATE,
            rolling_update_configuration=rolling_update_config,
            associate_public_ip_address=True)
        asg_managers.add_security_group(sg)

        notification_target = as_ht.QueueHook(queue)
        asg_managers.add_lifecycle_hook(
            "Managers", lifecycle_transition=autoscaling.LifecycleTransition.INSTANCE_TERMINATING,
            default_result=autoscaling.DefaultResult.ABANDON, notification_target=notification_target)

        core.Tag.add(asg_managers, 'swarm-node-type', 'Manager')
        core.Tag.add(asg_managers, 'LogGroup', log_group.log_group_name)
        core.Tag.add(asg_managers, 'swarm-state-bucket', swarm_bucket.bucket_name)
        core.Tag.add(asg_managers, 'swarm-notification-queue', queue.queue_url)

        asg_workers = autoscaling.AutoScalingGroup(
            self,
            "WorkersLinux",
            vpc=vpc,
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3_AMD, ec2.InstanceSize.MICRO),
            machine_image=ec2.AmazonLinuxImage(generation=ec2.AmazonLinuxGeneration.AMAZON_LINUX_2),
            key_name=ssh_key,
            user_data=user_data,
            min_capacity=2,
            vpc_subnets=ec2.SubnetSelection(subnets=vpc.private_subnets),
            role=role,
            update_type=autoscaling.UpdateType.ROLLING_UPDATE,
            rolling_update_configuration=rolling_update_config,
            associate_public_ip_address=False)
        asg_workers.add_security_group(sg)
        asg_workers.add_lifecycle_hook(
            "Workers", lifecycle_transition=autoscaling.LifecycleTransition.INSTANCE_TERMINATING,
            default_result=autoscaling.DefaultResult.ABANDON, notification_target=notification_target)

        core.Tag.add(asg_workers, 'swarm-node-type', 'Worker')
        core.Tag.add(asg_workers, 'LogGroup', log_group.log_group_name)
        core.Tag.add(asg_workers, 'swarm-state-bucket', swarm_bucket.bucket_name)
        core.Tag.add(asg_workers, 'swarm-notification-queue', queue.queue_url)
