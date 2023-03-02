import boto3
import random
import json
import botocore
from datetime import datetime
import logging
import os

# Set up our logger
default_log_args = {
    "level": logging.DEBUG if os.environ.get("DEBUG", False) else logging.INFO,
    "format": "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    "datefmt": "%d-%b-%y %H:%M",
    "force": True,
}
logging.basicConfig(**default_log_args)
logger = logging.getLogger("Run-Lambda")


client_route53 =boto3.client('route53')

client_ec2 = boto3.client('ec2')

client_ddb = boto3.client('dynamodb')


# function to create mapping dns_record
def create_record(dns_name,public_ip):
    
    try:
        new_record_status = client_route53.change_resource_record_sets(
            ChangeBatch={
                    'Changes': [
                        {
                            'Action': 'CREATE',
                            'ResourceRecordSet': {
                                'Name':dns_name,
                                'ResourceRecords': [
                                    {
                                        'Value': public_ip ,
                                    },
                                ],
                                'TTL': 60,
                                'Type': 'A',
                            },
                       },
                    ],
            },
            HostedZoneId='Z1XH6UGAQ2GDGX',
        )
    except botocore.exceptions.ClientError as error:
    # Put your error handling logic here
        raise error
    
    logger.info("create dns record success:"+ new_record_status['ChangeInfo']['Id'])


    while True:
        try:
            logger.info("waiting dns:"+dns_name+"binding to be insync")
            response = client_route53.get_change(
                Id=new_record_status['ChangeInfo']['Id']
            )
            
            if response['ChangeInfo']['Status'] == 'INSYNC':
                logger.info("dns:"+dns_name+" already become insync state")
                break
        except botocore.exceptions.ClientError as error:
        # Put your error handling logic here
            raise error
    return 


# Due to instance state change can not pass tag, we need first verify whether it is our target instance 
def eligble_instance(InstanceID):
    filters=[
        {
            'Name': 'tag:karpenter.sh/provisioner-name',
            'Values': [
                'default'
            ]
        },
    ]
    response = client_ec2.describe_instances(InstanceIds=[InstanceID],Filters=filters)


    network_interface_id = response['Reservations'][0]['Instances'][0]['NetworkInterfaces'][0]['NetworkInterfaceId']
    privateIpAddress = response['Reservations'][0]['Instances'][0]['NetworkInterfaces'][0]['PrivateIpAddress']

    Instance_info={
        'network_interface_id': network_interface_id,
        'privateIpAddress': privateIpAddress
    }

    
    if response['Reservations']:
       return Instance_info
    else:
       return False

# Persist mapping relationship for broadcast schduling

def update_record_ddb(EIP, DNS_NAME, AssociationId,Status):
    table_name = 'record_table'

    try:
        response = client_ddb.put_item(
                    Item={
                        'EIP': {
                            'S': EIP,
                        },
                        'DNS_Record': {
                            'S': DNS_NAME,
                        },
                        'AssociationId': {
                            'S': AssociationId,
                        },
                        'allocated': {
                            'BOOL': Status,
                        },
                    },
                    ConditionExpression="attribute_not_exists(EIP)",
                    ReturnConsumedCapacity='TOTAL',
                    TableName=table_name
                )
    except botocore.exceptions.ClientError as error:
        logger.error("this eip:"+EIP+"has already exist in our records, please take some actions ")
        raise error

    return True


def lambda_handler(event, context):
    suffix = "zhenyu-swa.info"
    associated_times = 0
    
    # production
    instance_id = event['detail']['instance-id']
    # test
    #instance_id = event['instanceid']
    
    Instance_info = eligble_instance(instance_id)

    if not Instance_info:
        return {
            'statusCode': 200,
            'body': json.dumps('not a valid instance')
        }

    filters=[
            {'Name':'tag:Pool', 'Values': ['byol']},
            {'Name':'tag:App', 'Values': ['streaming']},
            {'Name':'tag:status', 'Values': ['unassociated']}
    ]
    addresses_dict = client_ec2.describe_addresses(Filters=filters)

    if len(addresses_dict['Addresses'])<1:
            logger.error("you are run out of EIP")
            return {
                'statusCode': 400,
                'body': json.dumps('you are run out of EIP')
            }
    while True:
        seeds = random.randint(0,len(addresses_dict['Addresses'])-1)
        eip_dict =  addresses_dict['Addresses'][seeds]
        eip = eip_dict['PublicIp']
        try:
            associate_result = client_ec2.associate_address(
                NetworkInterfaceId=Instance_info['network_interface_id'],
                AllocationId=eip_dict['AllocationId'],
                AllowReassociation=False,
                PrivateIpAddress=Instance_info['privateIpAddress']
            )
            associationId = associate_result['AssociationId']
        except botocore.exceptions.ClientError as error:
            logger.error("EIP:"+eip+" associated with instance:"+instance_id+ " failed")
            raise error
        if  associationId:
            logger.info("EIP:"+eip+" associated with instance:"+instance_id+ " successfully")
            response = client_ec2.create_tags(
                Resources=[
                    eip_dict['AllocationId'],
                ],
                Tags=[
                    {
                        'Key': 'status',
                        'Value': 'associated'
                    },
                ]
            )
            dns_name = eip+ "."+ suffix
            record_result = create_record(dns_name, eip)
            logger.info("instance successfully bidnging with EIP:"+ eip +" and DNS name:"+ dns_name)
            if  update_record_ddb(eip, dns_name, associationId, False):
                logger.info("update into record_table success")
            break
        else:
            associated_times +=1
            logger.info("associate failed, retry one more time，sumed failed times = "+ associate_times)
        
        return {
            'statusCode': 200,
            'body': json.dumps('successfully binding related EIP:'+ eip+ " with route53 record:"+ dns_name)
        }
