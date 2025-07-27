import argparse
import yaml
import boto3
import sys
import os
import time
import paramiko
import warnings
from getpass import getpass

# Suppress common Paramiko warnings
warnings.filterwarnings(action='ignore', module='.*paramiko.*')

# --- Conditional Imports for Platform-Specific Libraries ---
try:
    import libvirt
    from libvirt import libvirtError
except ImportError:
    libvirt = None
try:
    from proxmoxer import ProxmoxAPI
except ImportError:
    ProxmoxAPI = None
try:
    from pyVim import connect
    from pyVmomi import vim
except ImportError:
    connect = None
try:
    import wmi
except ImportError:
    wmi = None

# --- Helper Functions (same as before) ---
def parse_config(file_path):
    try:
        with open(os.path.expanduser(file_path), 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Error: Config file not found at '{file_path}'")
        sys.exit(1)

def ask_yes_no(question):
    while True:
        response = input(f"{question} [y/n]: ").lower().strip()
        if response in ['y', 'yes']:
            return True
        if response in ['n', 'no']:
            return False

def run_ssh_commands(hostname, ssh_config, commands):
    if not commands:
        return
    print(f"  - Connecting to {hostname} as user '{ssh_config['user']}'...")
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh_args = {'hostname': hostname, 'username': ssh_config['user'], 'port': 22, 'timeout': 20}
        key_path = ssh_config.get('key_path')
        password = ssh_config.get('password')
        if password:
            ssh_args['password'] = password
        elif key_path:
            ssh_args['key_filename'] = os.path.expanduser(key_path)
        
        ssh_client.connect(**ssh_args)
        for command in commands:
            print(f"    - Executing: '{command}'")
            stdin, stdout, stderr = ssh_client.exec_command(command, timeout=300)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status == 0:
                print(f"      - Success.")
            else:
                print(f"      - Error (Exit Code {exit_status}): {stderr.read().decode().strip()}")
                break
    except Exception as e:
        print(f"  - SSH connection or command execution failed: {e}")
    finally:
        ssh_client.close()

# --- Base Provisioner ---
class BaseProvisioner:
    def __init__(self, config, args):
        self.config = config
        self.args = args
        self.creds = config.get('credentials', {})
        self.ssh_defaults = config.get('ssh_defaults', {})

    def provision(self):
        raise NotImplementedError

    def get_ssh_config(self, vm_config):
        final_ssh = self.ssh_defaults.copy()
        final_ssh.update(vm_config.get('ssh_defaults', {}))
        # Prompt for password if not provided
        if not final_ssh.get('key_path') and not final_ssh.get('password'):
            try:
                final_ssh['password'] = getpass(f"Enter SSH password for user '{final_ssh.get('user', 'default')}': ")
            except KeyboardInterrupt:
                print("\nCancelled.")
                return None
        return final_ssh

# --- Provisioner Implementations ---

class AWSProvisioner(BaseProvisioner):
    # (Implementation is unchanged from previous version)
    def provision(self):
        print("--- Starting AWS Provisioning ---")
        ec2 = boto3.client('ec2', region_name=self.args.aws_region)
        for vm in self.config.get('aws', []):
            print(f"--> Provisioning AWS VM: {vm['name']}")
            try:
                instance_res = ec2.run_instances(
                    ImageId=vm['ami_id'], InstanceType=vm['instance_type'], MinCount=1, MaxCount=1,
                    KeyName=vm['key_name'], SecurityGroupIds=vm['network']['security_group_ids'],
                    SubnetId=vm['network']['subnet_id'],
                    TagSpecifications=[{'ResourceType': 'instance', 'Tags': [{'Key': 'Name', 'Value': vm['name']}]}]
                )
                instance_id = instance_res['Instances'][0]['InstanceId']
                print(f"  - Instance '{vm['name']}' creation started with ID: {instance_id}")
                waiter = ec2.get_waiter('instance_running')
                waiter.wait(InstanceIds=[instance_id])
                print(f"  - Instance {instance_id} is now running.")
                desc = ec2.describe_instances(InstanceIds=[instance_id])
                public_ip = desc['Reservations'][0]['Instances'][0].get('PublicIpAddress')
                if public_ip and vm.get('post_install'):
                    time.sleep(45) # Wait for cloud-init and sshd
                    ssh_config = self.get_ssh_config(vm)
                    if ssh_config:
                        run_ssh_commands(public_ip, ssh_config, vm['post_install'])
            except Exception as e:
                print(f"  - Error creating instance '{vm['name']}': {e}")
        print("--- AWS Provisioning Complete ---")

class ProxmoxProvisioner(BaseProvisioner):
    def provision(self):
        if not ProxmoxAPI: print("Error: proxmoxer library not found."); return
        p_creds = self.creds.get('proxmox', {})
        try:
            api = ProxmoxAPI(p_creds['host'], user=p_creds['user'], password=p_creds['password'], verify_ssl=False)
            node = api.nodes(p_creds['node'])
        except Exception as e: print(f"Error connecting to Proxmox: {e}"); return
        
        print("--- Starting Proxmox Provisioning ---")
        for vm in self.config.get('proxmox', []):
            print(f"--> Provisioning Proxmox VM: {vm['name']}")
            try:
                # Check if bridge exists
                if not any(n['iface'] == vm['network']['bridge'] for n in node.network.get()):
                    print(f"  - Error: Network bridge '{vm['network']['bridge']}' not found on node '{p_creds['node']}'. Please create it manually."); continue
                
                # Clone template
                print(f"  - Cloning from template '{vm['template']}' to VM ID {vm['vm_id']}...")
                clone_task = node.qemu(vm['template']).clone.create(newid=vm['vm_id'], name=vm['name'], full=1)
                self._wait_for_task(api, clone_task)

                # Configure and start
                vm_ref = node.qemu(vm['vm_id'])
                vm_ref.config.put(cores=vm['cpu'], memory=vm['ram'])
                vm_ref.status.start.post()
                print(f"  - VM '{vm['name']}' created and started.")

                if vm.get('post_install'):
                    ip = self._get_vm_ip(vm_ref)
                    if ip:
                        ssh_config = self.get_ssh_config(vm)
                        if ssh_config:
                            print(f"  - Found IP: {ip}. Waiting for SSH service..."); time.sleep(20)
                            run_ssh_commands(ip, ssh_config, vm['post_install'])
                    else:
                        print("  - Could not retrieve IP from Guest Agent. Skipping post-install.")
            except Exception as e:
                print(f"  - Error provisioning Proxmox VM '{vm['name']}': {e}")

    def _wait_for_task(self, api, task_id):
        while True:
            status = api.cluster.tasks(task_id).status.get()
            if status['status'] == 'stopped':
                if status.get('exitstatus') == 'OK':
                    print("  - Task completed successfully.")
                    return
                else:
                    raise Exception(f"Task failed with status: {status.get('exitstatus')}")
            time.sleep(2)

    def _get_vm_ip(self, vm_ref):
        for _ in range(15): # Try for ~2.5 minutes
            try:
                ip_data = vm_ref.agent.get('network-get-interfaces')
                for iface in ip_data['result']:
                    if iface.get('ip-addresses'):
                        for ip in iface['ip-addresses']:
                            if ip['ip-address-type'] == 'ipv4' and not ip['ip-address'].startswith('127.'):
                                return ip['ip-address']
            except Exception: pass
            time.sleep(10)
        return None

class VMwareProvisioner(BaseProvisioner):
    def provision(self):
        if not connect: print("Error: pyvmomi library not found."); return
        v_creds = self.creds.get('vmware', {})
        try:
            si = connect.SmartConnectNoSSL(host=v_creds['host'], user=v_creds['user'], pwd=v_creds['password'])
            content = si.RetrieveContent()
        except Exception as e: print(f"Error connecting to vSphere: {e}"); return

        print("--- Starting VMware Provisioning ---")
        dc = next((d for d in content.rootFolder.childEntity if d.name == v_creds['datacenter']), None)
        if not dc: print(f"Error: Datacenter '{v_creds['datacenter']}' not found."); return
        
        for vm_def in self.config.get('vmware', []):
            print(f"--> Provisioning VMware VM: {vm_def['name']}")
            try:
                cluster = next(c for c in dc.hostFolder.childEntity if c.name == vm_def['cluster'])
                template = self._find_obj(content, vim.VirtualMachine, vm_def['template'])
                datastore = next(d for d in dc.datastore if d.name == vm_def['datastore'])
                
                # Network handling
                net_name = vm_def['network']['port_group']
                network = self._find_obj(content, vim.Network, net_name)
                if not network and vm_def['network'].get('create_if_missing'):
                    if ask_yes_no(f"Network '{net_name}' not found. Create a new vSwitch and Port Group?"):
                        host = cluster.host[0] # Create on the first host of the cluster
                        host.configManager.networkSystem.AddVirtualSwitch(vswitchName=f"vSwitch_{net_name}", spec=vim.host.VirtualSwitch.Specification())
                        host.configManager.networkSystem.AddPortGroup(portgrp=vim.host.PortGroup.Specification(name=net_name, vlanId=0, vswitchName=f"vSwitch_{net_name}"))
                        print(f"  - Created vSwitch and Port Group '{net_name}'.")
                        network = self._find_obj(content, vim.Network, net_name)
                    else:
                        print("  - Skipping VM due to missing network."); continue

                relospec = vim.vm.RelocateSpec(datastore=datastore, pool=cluster.resourcePool)
                clonespec = vim.vm.CloneSpec(location=relospec, powerOn=False, template=False)
                task = template.Clone(folder=dc.vmFolder, name=vm_def['name'], spec=clonespec)
                self._wait_for_task(task)
                
                # Power on and run post-install
                new_vm = self._find_obj(content, vim.VirtualMachine, vm_def['name'])
                new_vm.PowerOn()
                print(f"  - VM '{vm_def['name']}' created and powered on.")
                
                if vm_def.get('post_install'):
                    ip = self._get_vm_ip(new_vm)
                    if ip:
                        ssh_config = self.get_ssh_config(vm_def)
                        if ssh_config:
                            print(f"  - Found IP: {ip}. Waiting for SSH service..."); time.sleep(30)
                            run_ssh_commands(ip, ssh_config, vm_def['post_install'])
                    else:
                        print("  - Could not retrieve IP from VMware Tools. Skipping post-install.")
            except Exception as e:
                print(f"  - Error provisioning VMware VM '{vm_def['name']}': {e}")
        connect.Disconnect(si)

    def _find_obj(self, content, vim_type, name):
        obj_view = content.viewManager.CreateContainerView(content.rootFolder, [vim_type], True)
        obj = next((item for item in obj_view.view if item.name == name), None)
        obj_view.Destroy()
        return obj

    def _wait_for_task(self, task):
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(2)
        if task.info.state == vim.TaskInfo.State.error:
            raise Exception(f"vSphere task failed: {task.info.error.msg}")
        print("  - vSphere task completed successfully.")

    def _get_vm_ip(self, vm):
        for _ in range(15):
            if vm.guest.ipAddress:
                return vm.guest.ipAddress
            time.sleep(10)
        return None

class KVMProvisioner(BaseProvisioner):
    # (Implementation is unchanged from previous version)
    def provision(self):
        if not libvirt: print("Error: libvirt-python not installed."); return
        print("--- Starting KVM Provisioning ---")
        try:
            h_creds = self.creds.get('kvm', {})
            user = h_creds.get('user', self.args.user)
            host = h_creds.get('host', self.args.host)
            conn_uri = f"qemu+ssh://{user}@{host}/system" if host else "qemu:///system"
            conn = libvirt.open(conn_uri)
        except Exception as e: print(f"Error connecting to KVM host '{conn_uri}': {e}"); return

        for vm in self.config.get('kvm', []):
            print(f"--> Provisioning KVM VM: {vm['name']}")
            net_name = vm['network']['source']
            try: conn.networkLookupByName(net_name)
            except libvirtError:
                if vm['network'].get('create_if_missing') and ask_yes_no(f"Network '{net_name}' not found. Create it?"):
                    net_xml=f"<network><name>{net_name}</name><forward mode='nat'/><bridge name='virbr_{net_name}' stp='on' delay='0'/><ip address='192.168.122.1' netmask='255.255.255.0'><dhcp><range start='192.168.122.128' end='192.168.122.254'/></dhcp></ip></network>"
                    net = conn.networkDefineXML(net_xml); net.create(); net.setAutostart(True)
                    print(f"  - Network '{net_name}' created.")
                else: print(f"  - Network '{net_name}' not found. Skipping VM."); continue
            
            disk_path = vm['disk']['path'] # This check is local, requires pre-setup on remote host
            if not os.path.exists(os.path.expanduser(disk_path)):
                if ask_yes_no(f"Disk '{disk_path}' not found. Create it?"):
                    os.system(f"qemu-img create -f qcow2 {disk_path} {vm['disk']['size']}")
                else: print("  - Skipping VM due to missing disk."); continue

            xml_config = f"""<domain type='kvm'><name>{vm['name']}</name><memory unit='MiB'>{vm['ram']}</memory><vcpu>{vm['cpu']}</vcpu><os><type>hvm</type><boot dev='hd'/><boot dev='cdrom'/></os><devices><disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='{disk_path}'/><target dev='vda' bus='virtio'/></disk><disk type='file' device='cdrom'><driver name='qemu' type='raw'/><source file='{vm["iso_path"]}'/><target dev='sda' bus='sata'/><readonly/></disk><interface type='network'><source network='{net_name}'/><model type='virtio'/></interface><graphics type='vnc' port='-1' autoport='yes' listen='0.0.0.0'/><channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0'/></channel></devices></domain>"""
            try:
                dom = conn.defineXML(xml_config); dom.create()
                print(f"  - VM '{vm['name']}' created and started.")
                if vm.get('post_install'):
                    ip = self._get_vm_ip(dom)
                    if ip:
                        ssh_config = self.get_ssh_config(vm)
                        if ssh_config:
                            print(f"  - Found IP: {ip}. Waiting for SSH service..."); time.sleep(45)
                            run_ssh_commands(ip, ssh_config, vm['post_install'])
                    else: print("  - Could not retrieve IP from guest agent. Is the OS installed and agent running?");
            except libvirtError as e: print(f"  - Error creating KVM VM '{vm['name']}': {e}")
        conn.close()

    def _get_vm_ip(self, dom):
        for _ in range(15):
            try:
                ifaces = dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT, 0)
                for name, val in ifaces.items():
                    for ipaddr in val['addrs']:
                        if ipaddr['type'] == libvirt.VIR_IP_ADDR_TYPE_IPV4 and not ipaddr['addr'].startswith('127.'):
                            return ipaddr['addr']
            except libvirtError: pass
            time.sleep(10)
        return None

class HyperVProvisioner(BaseProvisioner):
    def provision(self):
        if not wmi: print("Error: wmi library not found."); return
        if sys.platform != "win32": print("Error: Hyper-V provisioning only works on Windows."); return
        
        h_creds = self.creds.get('hyperv', {})
        wmi_args = {"namespace": r"root\virtualization\v2"}
        if h_creds.get('host'):
            wmi_args.update({'computer': h_creds['host'], 'user': h_creds['user'], 'password': h_creds['password']})

        try:
            conn = wmi.WMI(**wmi_args)
            vms_service = conn.Msvm_VirtualSystemManagementService()[0]
        except Exception as e: print(f"Error connecting to Hyper-V: {e}"); return
        
        print("--- Starting Hyper-V Provisioning ---")
        for vm in self.config.get('hyperv', []):
            print(f"--> Provisioning Hyper-V VM: {vm['name']}")
            try:
                # Network check/creation
                switch_name = vm['network']['switch_name']
                switch = conn.Msvm_VirtualEthernetSwitch(ElementName=switch_name)
                if not switch:
                    if vm['network'].get('create_if_missing') and ask_yes_no(f"vSwitch '{switch_name}' not found. Create it?"):
                        switch_type_map = {'Internal': 1, 'External': 2, 'Private': 0}
                        res, job = vms_service.CreateSwitch(FriendlyName=switch_name, SwitchType=switch_type_map.get(vm['network']['switch_type'], 1))
                        self._wait_for_job(conn, job); print(f"  - Created vSwitch '{switch_name}'.")
                    else: print("  - Skipping VM due to missing network."); continue
                
                # Create VM
                vm_settings = conn.Msvm_VirtualSystemSettingData.new(); vm_settings.ElementName = vm['name']
                vm_settings.VirtualSystemType = 'Microsoft:Hyper-V:SubType:2' if vm.get('generation') == 2 else 'Microsoft:Hyper-V:SubType:1'
                res, vm_path, job = vms_service.DefineSystem(SystemSettings=vm_settings.GetText_(1))
                if res != 0: raise Exception(f"DefineSystem failed with code {res}")
                
                vm_obj = wmi.WMI(moniker=vm_path); vm_config = vm_obj.associators(wmi_result_class='Msvm_VirtualSystemSettingData')[0]
                # CPU/RAM
                vm_config.associators(wmi_result_class='Msvm_ProcessorSettingData')[0].VirtualQuantity = vm['cpu']
                vm_config.associators(wmi_result_class='Msvm_MemorySettingData')[0].VirtualQuantity = vm['ram']
                res, job = vms_service.ModifySystemSettings(SystemSettings=vm_config.GetText_(1)); self._wait_for_job(conn, job)

                # TODO: Add disk and network adapter creation logic here
                print(f"  - VM '{vm['name']}' defined. Disk/NIC attachment not yet implemented.")
                
            except Exception as e:
                print(f"  - Error provisioning Hyper-V VM '{vm['name']}': {e}")
                
    def _wait_for_job(self, conn, job_path):
        job = wmi.WMI(moniker=job_path)
        while job.JobState == 4: # 4 = Running
            time.sleep(1)
            job = wmi.WMI(moniker=job_path)
        if job.JobState != 7: # 7 = Completed
            raise Exception(f"Hyper-V job failed with state {job.JobState}. Error: {job.ErrorDescription}")

# --- Main Execution Logic ---
def main():
    parser = argparse.ArgumentParser(description="Advanced Automated Lab Provisioner (ALP)")
    parser.add_argument("platform", choices=["aws", "proxmox", "vmware", "kvm", "hyperv"], help="The platform to provision resources on.")
    parser.add_argument("--config", default="config.yaml", help="Path to the YAML configuration file.")
    parser.add_argument("--host", help="Remote hypervisor host (for KVM, Hyper-V). Overrides config file.")
    parser.add_argument("--user", help="Remote hypervisor user (for KVM, Hyper-V). Overrides config file.")
    parser.add_argument("--aws-region", default=os.environ.get("AWS_REGION", "us-east-1"), help="AWS region")
    args = parser.parse_args()
    
    config_data = parse_config(args.config)
    
    provisioner_map = {
        "aws": AWSProvisioner, "proxmox": ProxmoxProvisioner, "vmware": VMwareProvisioner,
        "kvm": KVMProvisioner, "hyperv": HyperVProvisioner,
    }
    provisioner_class = provisioner_map.get(args.platform)
    if provisioner_class:
        provisioner = provisioner_class(config_data, args)
        provisioner.provision()

if __name__ == "__main__":
    main()
