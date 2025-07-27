import argparse
import yaml
import boto3
import sys
import os
from proxmoxer import ProxmoxAPI

# Conditional imports for platform-specific libraries
try:
    import libvirt
except ImportError:
    print("Warning: 'libvirt-python' not found. KVM functionality will be unavailable.")
    libvirt = None

try:
    from pyVim import connect
    from pyVmomi import vim
except ImportError:
    print("Warning: 'pyvmomi' not found. VMware functionality will be unavailable.")
    connect = None

try:
    import wmi
except ImportError:
    print("Warning: 'wmi' not found. Hyper-V functionality will be unavailable.")
    wmi = None

# --- Section 1: Configuration Parser ---

def parse_config(file_path):
    """Parses the YAML configuration file."""
    try:
        with open(file_path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Error: Configuration file not found at '{file_path}'")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file: {e}")
        sys.exit(1)

# --- Section 2: Provisioning Functions ---

def provision_aws(config):
    """Provisions EC2 instances on AWS."""
    print("--- Starting AWS Provisioning ---")
    ec2 = boto3.client('ec2', region_name=os.environ.get("AWS_REGION", "us-east-1"))
    for vm in config.get('aws', []):
        try:
            print(f"Creating instance '{vm['name']}'...")
            ec2.run_instances(
                ImageId=vm['ami_id'],
                InstanceType=vm['instance_type'],
                MinCount=1,
                MaxCount=1,
                KeyName=vm['key_name'],
                SecurityGroupIds=[vm['security_group']],
                TagSpecifications=[{
                    'ResourceType': 'instance',
                    'Tags': [{'Key': 'Name', 'Value': vm['name']}]
                }]
            )
            print(f"Instance '{vm['name']}' creation request sent successfully.")
        except Exception as e:
            print(f"Error creating instance '{vm['name']}': {e}")
    print("--- AWS Provisioning Complete ---")


def provision_proxmox(config):
    """Provisions VMs on Proxmox."""
    print("--- Starting Proxmox Provisioning ---")
    try:
        proxmox = ProxmoxAPI(
            os.environ['PROXMOX_HOST'],
            user=os.environ['PROXMOX_USER'],
            password=os.environ['PROXMOX_PASSWORD'],
            verify_ssl=False
        )
    except KeyError as e:
        print(f"Error: Missing environment variable {e} for Proxmox connection.")
        sys.exit(1)

    node = os.environ.get("PROXMOX_NODE")
    if not node:
        print("Error: Missing PROXMOX_NODE environment variable.")
        sys.exit(1)
        
    for vm in config.get('proxmox', []):
        try:
            print(f"Creating VM '{vm['name']}' with ID {vm['vm_id']}...")
            proxmox.nodes(node).qemu.create(
                vmid=vm['vm_id'],
                name=vm['name'],
                memory=vm['ram'],
                cores=vm['cpu'],
                net0='virtio,bridge=vmbr0',
                ide2=vm['iso_path'] + ',media=cdrom',
                scsihw='virtio-scsi-pci',
                virtio0=f"local-lvm:{vm['disk']}",
                boot='order=ide2;scsi0'
            )
            print(f"VM '{vm['name']}' creation request sent successfully.")
        except Exception as e:
            print(f"Error creating Proxmox VM '{vm['name']}': {e}")
    print("--- Proxmox Provisioning Complete ---")


def provision_vmware(config):
    """Provisions VMs on VMware vSphere."""
    if not connect:
        print("Error: pyvmomi is not installed. Cannot proceed with VMware provisioning.")
        return
        
    print("--- Starting VMware Provisioning ---")
    try:
        si = connect.SmartConnectNoSSL(
            host=os.environ['VMWARE_HOST'],
            user=os.environ['VMWARE_USER'],
            pwd=os.environ['VMWARE_PASSWORD'],
            port=443
        )
    except KeyError as e:
        print(f"Error: Missing environment variable {e} for VMware connection.")
        sys.exit(1)
    except Exception as e:
        print(f"Error connecting to VMware vSphere: {e}")
        sys.exit(1)

    content = si.RetrieveContent()

    for vm_def in config.get('vmware', []):
        try:
            print(f"Cloning VM '{vm_def['name']}' from template '{vm_def['template']}'...")
            
            # Find objects in vCenter
            dc = next((d for d in content.rootFolder.childEntity if d.name == vm_def['datacenter']), None)
            template = next(vm for vm in dc.vmFolder.childEntity if vm.name == vm_def['template'])
            cluster = next((c for c in dc.hostFolder.childEntity if c.name == vm_def['cluster']), None)
            datastore = next((d for d in dc.datastore if d.name == vm_def['datastore']), None)

            # Relocation spec
            relospec = vim.vm.RelocateSpec(datastore=datastore, pool=cluster.resourcePool)
            
            # Clone spec
            clonespec = vim.vm.CloneSpec(location=relospec, powerOn=False, template=False)
            
            task = template.Clone(folder=dc.vmFolder, name=vm_def['name'], spec=clonespec)
            print(f"Clone task for '{vm_def['name']}' started.")
        except Exception as e:
            print(f"Error creating VMware VM '{vm_def['name']}': {e}")
            
    connect.Disconnect(si)
    print("--- VMware Provisioning Complete ---")


def provision_kvm(config):
    """Provisions VMs on KVM using libvirt."""
    if not libvirt:
        print("Error: libvirt-python is not installed. Cannot proceed with KVM provisioning.")
        return
        
    print("--- Starting KVM Provisioning ---")
    try:
        conn = libvirt.open('qemu:///system')
    except libvirt.libvirtError as e:
        print(f"Error connecting to KVM/libvirt: {e}")
        sys.exit(1)

    for vm in config.get('kvm', []):
        print(f"Defining KVM VM '{vm['name']}'...")
        
        xml_config = f"""
        <domain type='kvm'>
          <name>{vm['name']}</name>
          <memory unit='MiB'>{vm['ram']}</memory>
          <vcpu placement='static'>{vm['cpu']}</vcpu>
          <os>
            <type arch='x86_64' machine='pc-q35-6.2'>hvm</type>
            <boot dev='cdrom'/>
          </os>
          <devices>
            <disk type='file' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source file='/var/lib/libvirt/images/{vm['name']}.qcow2'/>
              <target dev='vda' bus='virtio'/>
            </disk>
            <disk type='file' device='cdrom'>
                <driver name='qemu' type='raw'/>
                <source file='{vm["iso_path"]}'/>
                <target dev='hda' bus='ide'/>
                <readonly/>
            </disk>
            <interface type='network'>
              <source network='default'/>
              <model type='virtio'/>
            </interface>
            <graphics type='vnc' port='-1' autoport='yes' listen='0.0.0.0'>
                <listen type='address' address='0.0.0.0'/>
            </graphics>
          </devices>
        </domain>
        """
        
        try:
            # Note: This just defines the VM. Creating the disk image is a separate step.
            # os.system(f"qemu-img create -f qcow2 /var/lib/libvirt/images/{vm['name']}.qcow2 {vm['disk']}")
            print(f"Note: Ensure disk image exists at /var/lib/libvirt/images/{vm['name']}.qcow2")
            dom = conn.defineXML(xml_config)
            # dom.create() # Uncomment to start the VM immediately
            print(f"VM '{vm['name']}' defined successfully. Start it manually via 'virsh'.")
        except libvirt.libvirtError as e:
            print(f"Error defining KVM VM '{vm['name']}': {e}")
            
    conn.close()
    print("--- KVM Provisioning Complete ---")


def provision_hyperv(config):
    """Provisions VMs on Hyper-V."""
    if not wmi:
        print("Error: wmi library not found. Cannot proceed with Hyper-V provisioning.")
        return
    if sys.platform != "win32":
        print("Error: Hyper-V provisioning is only supported on Windows.")
        sys.exit(1)

    print("--- Starting Hyper-V Provisioning ---")
    try:
        conn = wmi.WMI(computer=".", namespace=r"root\virtualization\v2")
        vm_service = conn.Msvm_VirtualSystemManagementService()[0]
    except Exception as e:
        print(f"Error connecting to Hyper-V WMI service: {e}")
        sys.exit(1)

    for vm in config.get('hyperv', []):
        print(f"Creating Hyper-V VM '{vm['name']}'...")
        vm_settings = conn.Msvm_VirtualSystemSettingData.new()
        vm_settings.ElementName = vm['name']

        # Create the VM
        result, vm_path, job_path = vm_service.DefineSystem(SystemSettings=vm_settings.GetText_(1))
        if result != 0:
            print(f"Error creating VM '{vm['name']}'. Result code: {result}")
            continue

        # Configure CPU and RAM
        vm_obj = wmi.WMI(moniker=vm_path)
        vm_config = vm_obj.associators(wmi_result_class='Msvm_VirtualSystemSettingData')[0]
        
        proc_config = vm_config.associators(wmi_result_class='Msvm_ProcessorSettingData')[0]
        proc_config.VirtualQuantity = vm['cpu']
        
        mem_config = vm_config.associators(wmi_result_class='Msvm_MemorySettingData')[0]
        mem_config.VirtualQuantity = vm['ram'] # In MB

        # Apply changes
        result, _ = vm_service.ModifySystemSettings(SystemSettings=vm_config.GetText_(1))
        if result == 0:
            print(f"VM '{vm['name']}' created and configured successfully.")
        else:
            print(f"Error applying settings to '{vm['name']}'.")

    print("--- Hyper-V Provisioning Complete ---")


# --- Section 3: Main Execution Logic ---

def main():
    """Main function to parse arguments and trigger provisioning."""
    parser = argparse.ArgumentParser(description="Automated Lab Provisioner (ALP)")
    parser.add_argument("platform", choices=["aws", "proxmox", "vmware", "kvm", "hyperv"],
                        help="The platform to provision resources on.")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to the YAML configuration file.")
    
    args = parser.parse_args()
    
    config_data = parse_config(args.config)
    
    if args.platform == "aws":
        provision_aws(config_data)
    elif args.platform == "proxmox":
        provision_proxmox(config_data)
    elif args.platform == "vmware":
        provision_vmware(config_data)
    elif args.platform == "kvm":
        provision_kvm(config_data)
    elif args.platform == "hyperv":
        provision_hyperv(config_data)

if __name__ == "__main__":
    main()
