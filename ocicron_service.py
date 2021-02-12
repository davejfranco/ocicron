import os
import oci
from tinydb import TinyDB, Query
from crontab import CronTab


DEFAULT_LOCATION=os.getcwd()
DB_FILE_NAME="scheduleDB.json"
TAG_KEYS={"Stop", "Start", "Weekend_stop"}
REGIONS=['us-ashburn-1', 'sa-santiago-1']
COMPARTMENTS=["ocid1.compartment.oc1..aaaaaaaa4bybtq6axk7odphukoulaqsq6zdewp7kgqunjxhw3icuohglhnwa"]
DEFAULT_AUTH_TYPE='config'
DEFAULT_PROFILE="ladmcrs"
DEFAULT_SYNC_SCHEDULE='0 23 1 * *'
DEFAULT_SYNC_COMMAND=DEFAULT_LOCATION + '/ocicron.py sync'
CRONTAB_FILE_NAME=os.path.join(os.getcwd(),'ocicron.tab')


class OCI:

    def __init__(self, auth_type, config_file="~/.oci/config", profile="DEFAULT", region=None):
        self.auth_type = auth_type
        self.config_file = config_file
        self.profile = profile
        self.region = region

        if self.auth_type == "principal":
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            if self.region is not None:
                self.compute = oci.core.ComputeClient(config={'region':self.region}, signer=signer)
                self.identity = oci.identity.IdentityClient(config={'region':self.region}, signer=signer)
            else:
                self.compute = oci.core.ComputeClient(config={}, signer=signer)
                self.identity = oci.identity.IdentityClient(config={}, signer=signer)
        elif self.auth_type == "config":
            self.config = oci.config.from_file(file_location=config_file, profile_name=profile)
            if self.region is not None:
                self.config['region'] = self.region
            self.compute = oci.core.ComputeClient(self.config)
            self.identity = oci.identity.IdentityClient(self.config)
        else:
            raise Exception("Unrecognize authentication type: auth_type=(principal|config)")
        
        self.compartment_ids = []
        self.compute_instances = []
    
    def _get_sub_compartment_ids(self, cid):
        
        if cid not in self.compartment_ids:
            self.compartment_ids.append(cid)

        result = self.identity.list_compartments(cid)
        if len(result.data) == 0:
            return self.compartment_ids
        else:
            for compartment in result.data:
                if compartment.lifecycle_state == "ACTIVE" and compartment.id not in self.compartment_ids:
                        self.compartment_ids.append(compartment.id)

    def compartment_crawler(self, parent_cid):
        
        self._get_sub_compartment_ids(parent_cid)
        for cid in self.compartment_ids:
            self._get_sub_compartment_ids(cid)
        return self.compartment_ids

    def get_all_instances(self):
        """
        Return all instances in a given compartment
        """
        if len(self.compartment_ids) <= 0:
            return

        for compartment_id in self.compartment_ids:   
            response = self.compute.list_instances(
                compartment_id=compartment_id,
                sort_by="TIMECREATED",
                sort_order="ASC"
            )
            #list instances with pagination
            vms = response.data
            while response.has_next_page:
                response = self.compute.list_instances(compartment_id, page=response.next_page)
                vms.extend(response.data)  

            for vm in vms:
                if vm.lifecycle_state == 'RUNNING' or vm.lifecycle_state == 'STOPPED':
                    self.compute_instances.append(vm)         
        return self.compute_instances
    
    def filter_by_tags(self, tags):
        """
        returns list if OCID of a given tags
        tags = {"Stop":"20","Start": "08","Weekly_stop":"Yes"}
        """
        vmOCID=[]
        for vm in self.compute_instances:
            #compare dictionary and length should be the same
            if len(tags.items() & vm.freeform_tags.items()) == len(tags.items()):
                vmOCID.append(vm.id)
        return vmOCID
    
    def _discover_tags(self, tag_keys=TAG_KEYS):
        """
        Discovery tag keys and values from compute freeform_tags

        example: discover_tag({"Stop", "Start", "Weekly_stop"})
        result: [{'Start': '08', 'Stop': '20', 'Weekly_stop': 'No'}, {'Start': '08', 'Stop': '20', 'Weekly_stop': 'Yes'}]
        """
        result = []
        vm_group = {}
        for vm in self.compute_instances:
            if len(tag_keys & vm.freeform_tags.keys()) == len(tag_keys):
                tags = {}
                for key in tag_keys:
                    tags[key] = vm.freeform_tags[key]
                if tags not in result:
                    result.append(tags)
        return result


    def vms_by_tags(self, tag_keys=TAG_KEYS):   

        tags = self._discover_tags()
        result = []
        for tag in tags:
            vm_group = {}
            vm_group["tags"] = tag
            vm_group["vmOCID"] = self.filter_by_tags(tag)
            result.append(vm_group)
        return result

    def instance_action(self, instances, action):
        """
        Perform a given intance action of a given list of VM OCID
        """
        for ocid in instances:
            self.compute.instance_action(ocid, action)

class ScheduleDB:

    def __init__(self, location=os.path.join(DEFAULT_LOCATION, DB_FILE_NAME)):
        self.location = location
        self.db = TinyDB(self.location)
        self.vm_table = self.db.table('vms')
        self.cid_table = self.db.table('compartments')
        self.cron_table = self.db.table('cron')

        #Query
        self.query = Query()

class Schedule:

    def __init__(self, tabfile=None):
        if tabfile is not None:
            self.tabfile = tabfile
            self.cron = CronTab(user=True, tabfile=self.tabfile)
        else:
            self.cron = CronTab(user=True)
    
    def new(self, command, schedule, comment=None):
        job = self.cron.new(command=command, comment=comment)
        
        job.setall(schedule)
        self.cron.write()
    
    @staticmethod
    def cron_generator(hour, weekend, region, action):
        """
        EJ: 0 20 * * * python ocicron.py --region us-ashburn-1 --action stop --at 09 --weekend-stop yes
        r['Stop'], False, region, 'stop'
        """
        #if weekend is True means should remains stopped all weekend
        if weekend == 'yes':
            return '0 {} * * 1-5'.format(hour), '{}/ocicron.py --region {} --action {} --at {} --weekend-stop {}'.format(DEFAULT_LOCATION, region, action, hour, weekend)
        else:
            return '0 {} * * *'.format(hour), '{}/ocicron.py --region {} --action {} --at {} --weekend-stop {}'.format(DEFAULT_LOCATION, region, action, hour, weekend)
    
    def is_schedule(self, command):
        """
        Find if a given schedule exists in crontab file
        """
        cron_commands = []
        for job in self.cron.find_command(command='ocicron.py'):
            cron_commands.append(job.command)
        
        if command in cron_commands:
            return True
        return False

    def clean_jobs(self, command):
        """
        Find commands in crontab and remove them
        """
        self.cron.remove_all(command=command)
        self.cron.write()