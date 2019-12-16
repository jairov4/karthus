#!/usr/bin/env python3

from aws_cdk import core

from karthus.karthus_stack import KarthusStack

ACCOUNT = '583161073698'
env = core.Environment(account=ACCOUNT, region="us-east-2")
app = core.App()
stack = KarthusStack(app, "karthus", env=env)
core.Tag.add(stack, 'Owner', 'jairo.velasco')
core.Tag.add(stack, 'Company', 'Trilogy')
core.Tag.add(stack, 'BU', 'ComputePlatform')
core.Tag.add(stack, 'EnvironmentType', 'Development')
core.Tag.add(stack, 'Purpose', 'PoC')
app.synth()
