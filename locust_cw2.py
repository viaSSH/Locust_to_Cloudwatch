# encoding: utf-8

from locust import HttpLocust, TaskSet, task, events, web, main
import time, datetime, logging, boto3, os, sys, json  #moved this line AFTER locust imports - https://github.com/gevent/gevent/issues/1016
import queue

import inspect

log = logging.getLogger()


#_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/
#CONFIG VALUES - feel free to update
CW_METRICS_NAMESPACE="concurrencylabs/loadtests/locust"
CW_LOGS_LOG_GROUP="LocustTests"
CW_LOGS_LOG_STREAM="load-generator"
#_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/_/

STATUS_SUCCESS = "SUCCESS"
STATUS_FAILURE = "FAILURE"

class CloudWatchConnector(object):

    def __init__(self, host, namespace, loggroup, logstream, iamrolearn):
        seq = datetime.datetime.now().microsecond
        self.loggroup = loggroup
        self.logstream = logstream + "_" + str(seq)
        self.namespace = namespace #the same namespace is used for both CW Logs and Metrics
        self.nexttoken = ""
        self.response_queue = queue.Queue()
        self.batch_size = 5
        self.host = host
        self.usercount = None
        self.iamrolearn = iamrolearn
        self.lastclientrefresh = None

        self.init_clients()



        if not self.loggroup_exists():
            self.cwlogsclient.create_log_group(logGroupName=self.loggroup)

        self.cwlogsclient.create_log_stream(logGroupName=self.loggroup,logStreamName=self.logstream)

    def init_clients(self):
        if self.iamrolearn:
            log.info("Initializing AWS SDK clients using IAM Role:[{}]".format(self.iamrolearn))
            stsclient = boto3.client('sts')
            stsresponse = stsclient.assume_role(RoleArn=self.iamrolearn, RoleSessionName='cwlocustconnector')
            if 'Credentials' in stsresponse:
                accessKeyId = stsresponse['Credentials']['AccessKeyId']
                secretAccessKey = stsresponse['Credentials']['SecretAccessKey']
                sessionToken = stsresponse['Credentials']['SessionToken']
                self.cwlogsclient = boto3.client('logs',aws_access_key_id=accessKeyId, aws_secret_access_key=secretAccessKey,aws_session_token=sessionToken)
                self.cwclient = boto3.client('cloudwatch',aws_access_key_id=accessKeyId, aws_secret_access_key=secretAccessKey,aws_session_token=sessionToken)

        else:
            log.info("Initializing AWS SDK clients using configured AWS credentials")
            self.cwlogsclient = boto3.client('logs')
            self.cwclient = boto3.client('cloudwatch')

        self.lastclientrefresh = datetime.datetime.now()


    def loggroup_exists(self):
        result = False
        response = self.cwlogsclient.describe_log_groups(logGroupNamePrefix=self.loggroup, limit=1)
        if len(response['logGroups']): result = True
        return result

    def on_request_success(self, request_type, name, response_time, response_length, **kwargs):
        request_result = RequestResult(self.host, request_type, name, response_time, response_length, "", STATUS_SUCCESS)
        # print("====> success")
        self.response_queue.put(request_result)

    def on_request_failure(self, request_type, name, response_time, exception, **kwargs):
        request_result = RequestResult(self.host, request_type, name, response_time, 0, exception, STATUS_FAILURE)
        self.response_queue.put(request_result)

    def on_dlt_complete(self, env):
        
        # print(inspect.getmembers(env.runner))
        # print(dir(env.runner))
        user_count = env.runner.target_user_count
        print("user count : ", user_count)
        self.usercount = UserCount(host=self.host, usercount=user_count)
        
        self.start_cw_loop()

    def start_cw_loop(self):
        print("start cw loop")
        while True:
            awsclientage = datetime.datetime.now() - self.lastclientrefresh
            #Refresh AWS clients every 50 minutes, if using an IAM Role (since credentials expire in 1hr)
            if self.iamrolearn and awsclientage.total_seconds() > 50*60 :
                self.init_clients()

            time.sleep(.1)
            batch = self.get_batch()
            cw_logs_batch = batch['cw_logs_batch']
            cw_metrics_batch = batch['cw_metrics_batch']
            if cw_logs_batch:
                try:
                    if self.nexttoken:
                        response = self.cwlogsclient.put_log_events(
                            logGroupName=self.loggroup, logStreamName=self.logstream,
                            logEvents=cw_logs_batch,
                            sequenceToken=self.nexttoken
                        )
                    else:
                        response = self.cwlogsclient.put_log_events(
                            logGroupName=self.loggroup, logStreamName=self.logstream,
                            logEvents=cw_logs_batch
                        )
                    if 'nextSequenceToken' in response: self.nexttoken = response['nextSequenceToken']
                except Exception as e:
                    log.error(str(e))

            if cw_metrics_batch:
                try:
                    cwresponse = self.cwclient.put_metric_data(Namespace=self.namespace,MetricData=cw_metrics_batch)
                    log.debug("PutMetricData response: [{}]".format(json.dumps(cwresponse, indent=4)))
                except Exception as e:
                    log.error(str(e))


    def get_batch(self):
        result = {}
        cw_logs_batch = []
        cw_metrics_batch = []
        if self.response_queue.qsize() >= self.batch_size:
            for i in range(0, self.batch_size):
                request_response = self.response_queue.get()
                cw_logs_batch.append(request_response.get_cw_logs_record())
                cw_metrics_batch.append(request_response.get_cw_metrics_status_record())
                cw_metrics_batch.append(request_response.get_cw_metrics_count_record())
                if request_response.get_cw_metrics_response_size_record():
                    cw_metrics_batch.append(request_response.get_cw_metrics_response_size_record())
                if self.usercount: cw_metrics_batch.append(self.usercount.get_metric_data())

                self.response_queue.task_done()
            log.debug("Queue size:["+str(self.response_queue.qsize())+"]")
        result['cw_logs_batch']=cw_logs_batch
        result['cw_metrics_batch']=cw_metrics_batch
        return result

class RequestResult(object):
    def __init__(self, host, request_type, name, response_time, response_length, exception, status):
        self.timestamp = datetime.datetime.utcnow()
        self.request_type = request_type
        self.name = name
        self.response_time = response_time
        self.response_length = response_length
        self.host = host
        self.exception = exception
        self.status = status

    def get_cw_logs_record(self):
        record = {}
        timestamp = datetime.datetime.utcnow().strftime("%Y/%m/%d %H:%M:%S.%f UTC")#example: 2016/02/08 16:51:05.123456 CST
        message = ""
        if self.status == STATUS_SUCCESS:
            message = '[timestamp={}] [host={}] [request_type={}] [name={}] [response_time={}] [response_length={}]'.format(timestamp, self.host, self.request_type, self.name, self.response_time, self.response_length)
        if self.status == STATUS_FAILURE:
            message = '[timestamp={}] [host={}] [request_type={}] [name={}] [response_time={}] [exception={}]'.format(timestamp, self.host, self.request_type, self.name, self.response_time, self.exception)
        record = {'timestamp': self.get_seconds()*1000,'message': message}
        return record

    def get_cw_metrics_status_record(self):
        dimensions = self.get_metric_dimensions()
        result = {}

        if self.status == STATUS_SUCCESS:
            result = {
                    'MetricName': 'ResponseTime_ms',
                    'Dimensions': dimensions,
                    'Timestamp': self.timestamp,
                    'Value': self.response_time,
                    'Unit': 'Milliseconds'

                  }
        if self.status == STATUS_FAILURE:
            result = {
                    'MetricName': 'FailureCount',
                    'Dimensions': dimensions,
                    'Timestamp': self.timestamp,
                    'Value': 1,
                    'Unit': 'Count'

                  }

        return result


    def get_cw_metrics_response_size_record(self):
        dimensions = self.get_metric_dimensions()

        result = {}

        if self.status == STATUS_SUCCESS:
            result = {
                    'MetricName': 'ResponseSize_Bytes',
                    'Dimensions': dimensions,
                    'Timestamp': self.timestamp,
                    'Value': self.response_length,
                    'Unit': 'Bytes'

                  }

        return result



    def get_cw_metrics_count_record(self):
        dimensions = self.get_metric_dimensions()
        result = {
                'MetricName': 'RequestCount',
                'Dimensions': dimensions,
                'Timestamp': self.timestamp,
                'Value': 1,
                'Unit': 'Count'
                  }
        return result



    def get_metric_dimensions(self):
        result =  [{'Name': 'Request','Value': self.name},{'Name': 'Host','Value': self.host}]
        return result


    def get_seconds(self):
        epoch = datetime.datetime.utcfromtimestamp(0)
        return int((self.timestamp - epoch).total_seconds())


class UserCount(object):
    def __init__(self, host,  usercount):
        self.host = host
        self.usercount = usercount

    def get_metric_data(self):
        result = []
        dimensions = [{'Name': 'Host','Value': self.host}]
        result = {
                'MetricName': 'UserCount','Dimensions': dimensions,'Timestamp': datetime.datetime.utcnow(),
                'Value': self.usercount, 'Unit': 'Count'
            }

        return result

@events.request.add_listener
def my_req_handler(request_type, name, response_time, response_length, response,
                       context, exception, start_time, url, **kwargs):

    # Failure
    if exception:
        # print(f"Request to {name} failed with exception {exception}")
        cwconn.on_request_failure(request_type, name, response_time, exception, **kwargs)
    # Success
    else:
        # print(f"Successfully made a request to: {name}")
        # print(f"The response was {response.text}")
        # print(f"The response len {len(response.text)}")
        cwconn.on_request_success(request_type, name, response_time, response_length, **kwargs)

@events.quitting.add_listener 
def dlt_complete_handler(environment, **kargs):
    cwconn.on_dlt_complete(environment)
    self.usercount = UserCount(host=self.host, usercount=user_count)
    self.start_cw_loop()

    print("dlt completed")


if __name__ == "__main__":

   #parser, options, arguments = main.parse_options()
#    parser, options = main.parse_options()

   host = 'http://exam.viassh.com'
#    if options.host:
#        host = options.host

   #this parameter is supported in case the load generator publishes metrics to CloudWatch in a different AWS account
   iamrolearn = 'arn:aws:iam::336481557929:role/ec2AndCloudwatchFull'

   
#    if 'IAM_ROLE_ARN' in os.environ:
#        iamrolearn = os.environ['IAM_ROLE_ARN']


   cwconn = CloudWatchConnector(host=host, namespace=CW_METRICS_NAMESPACE,loggroup=CW_LOGS_LOG_GROUP,logstream=CW_LOGS_LOG_STREAM, iamrolearn=iamrolearn)


   main.main()