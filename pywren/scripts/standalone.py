#!/usr/bin/env python

import pywren
import boto3
import click
import shutil
import os
import json
import zipfile
from glob2 import glob
import io
import time 
import botocore
from multiprocessing import Process
from threading import Thread
from pywren import wrenhandler
import logging
import watchtower
import subprocess
import math
import sys
import platform 
try:
    # For Python 3.0 and later
    from urllib.request import urlopen
except ImportError:
    # Fall back to Python 2's urllib2
    from urllib2 import urlopen

logger = logging.getLogger(__name__)


SQS_VISIBILITY_SEC = 10
PROCESS_SLEEP_DUR_SEC=2
AWS_REGION_DEBUG='us-west-2'
QUEUE_SLEEP_DUR_SEC=2
IDLE_TERMINATE_THRESHOLD = 0.95

INSTANCE_ID_URL = "http://169.254.169.254/latest/meta-data/instance-id"
def get_my_ec2_instance(aws_region):

    ec2 = boto3.resource('ec2', region_name=aws_region)

    instance_id =  urlopen(INSTANCE_ID_URL).read()
    instances = ec2.instances.filter(InstanceIds=[instance_id])


    for instance in instances:
        return instance

# def get_my_ec2_uptime():
#     instance = get_my_ec2_instance()

#     launch_time = instance.launch_time
#     time_delta =  datetime.datetime.now(launch_time.tzinfo) - launch_time
#     print launch_time, time_delta
#     #hour_frac = (time_delta.total_seconds() % 3600) / 3600

#     return time_delta.total_seconds()

def tags_to_dict(d):
    if d is None:
        return {}
    return {a['Key'] : a['Value'] for a in d}


def get_my_ec2_meta(instance):
    
    tags = tags_to_dict(instance.tags)

    r = {'public_dns_name' : instance.public_dns_name, 
         'public_ip_address' : instance.public_ip_address, 
         'instance_id': instance.id}
    r.update(tags)
    return r

def get_my_uptime():
    with open('/proc/uptime', 'r') as f:
        uptime_seconds = float(f.readline().split()[0])
        return uptime_seconds

def check_is_ec2():
    """
    last-minute check to make sure we are on EC2. 

    """
    try:
        instance_id =  urlopen(INSTANCE_ID_URL, timeout=3).read()
        return True
    except: 
        return False

def ec2_self_terminate(idle_time, uptime, message_count, in_minutes=0):
    if check_is_ec2():
        logger.info("self-terminating after idle for {:.0f} sec ({:.0f} s uptime), processed {:d} messages".format(idle_time, uptime, message_count))
        for h in logger.handlers:
            h.flush()

        subprocess.call("sudo shutdown -h +{:d}".format(in_minutes), shell=True) # slight delay
    else:
        logger.warn("attempted to self-terminate on non-EC2 instance. Check config")


def idle_granularity_valid(idle_terminate_granularity, 
                           queue_receive_message_timeout):
    return (1.0 - IDLE_TERMINATE_THRESHOLD)*idle_terminate_granularity >  (queue_receive_message_timeout)*1.1
    
def server_runner(aws_region, sqs_queue_name, 
                  max_run_time, run_dir, server_name, log_stream_prefix,
                  max_idle_time=None, 
                  idle_terminate_granularity = None, 
                  queue_receive_message_timeout=10):
    """
    Extract messages from queue and pass them off
    """

    sqs = boto3.resource('sqs', region_name=aws_region)
    
    # Get the queue
    queue = sqs.get_queue_by_name(QueueName=sqs_queue_name)
    local_message_i = 0
    last_processed_timestamp = time.time()

    terminate_thold_sec = (IDLE_TERMINATE_THRESHOLD * idle_terminate_granularity)
    terminate_window_sec = idle_terminate_granularity - terminate_thold_sec
    queue_receive_message_timeout = min(math.floor(terminate_window_sec/1.2), queue_receive_message_timeout)
    queue_receive_message_timeout = int(max(queue_receive_message_timeout, 1))
    if not idle_granularity_valid(idle_terminate_granularity, 
                              queue_receive_message_timeout):
        raise Exception("Idle time granularity window smaller than queue receive message timeout with headroom, instance will not self-terminate")
    message_count = 0
    idle_time = 0
    while(True):
        logger.debug("reading queue" )
        response = queue.receive_messages(WaitTimeSeconds=queue_receive_message_timeout)
        if len(response) > 0:
            m = response[0]
            logger.info("Dispatching message_id={}".format(m.message_id))
            
            process_message(m, local_message_i, max_run_time, run_dir, 
                            aws_region, server_name, log_stream_prefix)
            message_count += 1
            last_processed_timestamp = time.time()
            idle_time = 0
        else:
            idle_time = time.time() - last_processed_timestamp

            logger.debug("no message, idle for {:3.0f} sec".format(idle_time))

        # this is EC2_only
        if max_idle_time is not None and \
           idle_terminate_granularity is not None:
            if idle_time > max_idle_time:
                my_uptime = get_my_uptime()
                time_frac = (my_uptime % idle_terminate_granularity) 
                
                logger.debug("Instance has been up for {:.0f} and inactive for {:.0f} time_frac={:.0f} terminate_thold={:.0f}".format(my_uptime, 
                                                                                                                                      idle_time, 
                                                                                                                                      time_frac, 
                                                                                                                                      terminate_thold_sec))


                if time_frac > terminate_thold_sec:
                    logger.info("Instance has been up for {:.0f} and inactive for {:.0f}, terminating".format(my_uptime, 
                                                                                                              idle_time))
                    ec2_self_terminate(idle_time, my_uptime, message_count, in_minutes=1)
                    # sometimes these appear to hang, so we are skipping them and instead calling sys.exit
                    #for h in logger.handlers:
                    #    h.flush()
                    sys.exit(0)


def process_message(m, local_message_i, max_run_time, run_dir, 
                    aws_region, 
                    server_name, log_stream_prefix):

    logger.info("processing message_id={}".format(m.message_id))

    event = json.loads(m.body)
    call_id = event['call_id']
    callset_id = event['callset_id']

    extra_env_debug = event.get('extra_env', {})
    unique_job_id = "{}:{}:{}".format(m.message_id, call_id, callset_id)
    logger.info("processing message_id={} callset_id={} call_id={}".format(m.message_id, callset_id, call_id))

    # FIXME this is all for debugging
    if 'DEBUG_THROW_EXCEPTION' in extra_env_debug:
        m.delete()
        raise Exception("Debug exception")
    message_id = m.message_id

    # run this in a thread: pywren.wrenhandler.generic_handler(event)
    p =  Thread(target=job_handler, args=(event, local_message_i, 
                                           run_dir, aws_region, server_name, 
                                           log_stream_prefix))
    # is thread done
    p.start()
    pid = "THREAD" # p.pid
    logger.info("processing message_id={} callset_id={} call_id={} process_pid={}".format(m.message_id, callset_id, call_id, pid))
    start_time = time.time()

    run_time = time.time() - start_time
    last_visibility_update_time = time.time()
    while run_time < max_run_time:
        time_since_visibility_update = time.time() - last_visibility_update_time
        est_visibility_left = SQS_VISIBILITY_SEC - time_since_visibility_update
        if est_visibility_left < (PROCESS_SLEEP_DUR_SEC*1.5):
            logger.debug("{} - {:3.1f}s since last visibility update, setting to {:3.1f} sec".format(message_id, time_since_visibility_update, 
                                                                                                              SQS_VISIBILITY_SEC))
            last_visibility_update_time = time.time()
            response = m.change_visibility(VisibilityTimeout=SQS_VISIBILITY_SEC)


        if not p.is_alive():
            logger.debug("{} - attempting to join process".format(message_id))
            # FIXME will this join ever hang? 
            p.join()
            break
        else:
            logger.debug("{} - {:3.1f}s since visibility update, sleeping".format(message_id, time_since_visibility_update))
            time.sleep(PROCESS_SLEEP_DUR_SEC)

        run_time = time.time() - start_time

    # if p.exitcode is None:
    #     logger.warn("{} - attempting to manuall terminate process ".format(message_id))
    #     p.terminate()  # FIXME PRINT LOTS OF ERRORS HERE # FIXME does not work with thread
    #     logger.warn("{} - Had to manually terminate process ".format(message_id)) 
    logger.info("deleting message_id={} callset_id={} call_id={}".format(m.message_id, callset_id, call_id))


    m.delete()

def copy_runtime(tgt_dir):
    files = glob(os.path.join(pywren.SOURCE_DIR, "./*.py"))
    for f in files:
        shutil.copy(f, os.path.join(tgt_dir, os.path.basename(f)))

def job_handler(job, job_i, run_dir, aws_region, 
                server_name, log_stream_prefix, 
                extra_context = None, 
                delete_taskdir=True):
    """
    Run a deserialized job in run_dir

    Just for debugging
    """
    debug_pid = open("/tmp/pywren.scripts.standalone.{}.{}.log".format(os.getpid(), 
                                                                       time.time()), 'w')
    print "subprocess job_handler job i=", job_i, "pid=", os.getpid()
    session = boto3.session.Session(region_name=aws_region)
    # we do this here instead of in the global context 
    # because of how multiprocessing works
    handler = watchtower.CloudWatchLogHandler(send_interval=20, 
                                              log_group="pywren.standalone", 
                                              stream_name=log_stream_prefix + "-{logger_name}", 
                                              boto3_session=session,
                                              max_batch_count=10)
    log_format_str ='{} %(asctime)s - %(name)s - %(levelname)s - %(message)s'.format(server_name)

    formatter = logging.Formatter(log_format_str, "%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)


    wren_log = pywren.wrenhandler.logger # logging.getLogger('pywren.wrenhandler')
    wren_log.setLevel(logging.DEBUG)
    wren_log.propagate = 0
    wren_log.addHandler(handler)

    original_dir = os.getcwd()
    debug_pid.write("added log handler\n")
    
    task_run_dir = os.path.join(run_dir, str(job_i))
    shutil.rmtree(task_run_dir, True) # delete old modules
    os.makedirs(task_run_dir)
    copy_runtime(task_run_dir)


    context = {'jobnum' : job_i}
    if extra_context is not None:
        context.update(extra_context)

    os.chdir(task_run_dir)
    try:
        debug_pid.write("invoking generic_handler\n")

        wrenhandler.generic_handler(job, context)
    finally:
        debug_pid.write("generic handler finally\n")

        if delete_taskdir:
            shutil.rmtree(task_run_dir)
        os.chdir(original_dir)
    handler.flush()
    debug_pid.write("done and returning\n")




@click.command()
@click.option('--max_run_time', default=3600, 
              help='max run time for a job', type=int)
@click.option('--run_dir', default="/tmp/pywren.rundir", 
              help='directory to hold intermediate output')
@click.option('--aws_region', default="us-west-2", 
              help='aws region')
@click.option('--sqs_queue_name', default="pywren-queue", 
              help='queue')
@click.option('--max_idle_time', default=None, type=int, 
              help='maximum time for queue to remine idle before we try to self-terminate (sec)')
@click.option('--idle_terminate_granularity', default=None, type=int, 
              help="only terminate if we have been up for an integral number of this")
@click.option('--queue_receive_message_timeout', default=10, type=int, 
              help="longpoll timeout for getting sqs messages")
def server(aws_region, max_run_time, run_dir, sqs_queue_name, max_idle_time, 
           idle_terminate_granularity, queue_receive_message_timeout):
    
    session = boto3.session.Session(region_name=aws_region)

    # make boto quiet locally FIXME is there a better way of doing this? 
    logging.getLogger('boto').setLevel(logging.CRITICAL)
    logging.getLogger('boto3').setLevel(logging.CRITICAL)
    logging.getLogger('botocore').setLevel(logging.CRITICAL)
    

    if platform.node() != 'c65':
        
        instance = get_my_ec2_instance(aws_region)
        ec2_metadata = get_my_ec2_meta(instance)
        server_name = ec2_metadata['Name']
        log_stream_prefix = ec2_metadata['instance_id']
    else:
        server_name='c65'
        log_stream_prefix='millennium-c65'

    log_format_str ='{} %(asctime)s - %(name)s - %(levelname)s - %(message)s'.format(server_name)


    formatter = logging.Formatter(log_format_str, "%Y-%m-%d %H:%M:%S")


    handler = watchtower.CloudWatchLogHandler(send_interval=20, 
                                              log_group="pywren.standalone", 
                                              stream_name=log_stream_prefix + "-{logger_name}", 
                                              boto3_session=session,
                                              max_batch_count=10)

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    #config = pywren.wrenconfig.default()
    server_runner(aws_region, sqs_queue_name, 
                  max_run_time, os.path.abspath(run_dir), 
                  server_name, log_stream_prefix, 
                  max_idle_time, 
                  idle_terminate_granularity, 
                  queue_receive_message_timeout)

