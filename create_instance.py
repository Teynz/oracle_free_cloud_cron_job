import oci, os, sys, datetime

config = {
    "user":        os.environ["OCI_USER_OCID"],
    "tenancy":     os.environ["OCI_TENANCY_OCID"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "region":      os.environ["OCI_REGION"],
    "key_file":    os.path.expanduser("~/.oci/key.pem"),
}

SSH_PUB_KEY = os.environ["OCI_SSH_PUBLIC_KEY"]

ADS = [
    "zsnG:EU-FRANKFURT-1-AD-1",
    "zsnG:EU-FRANKFURT-1-AD-2",
    "zsnG:EU-FRANKFURT-1-AD-3",
]

compute = oci.core.ComputeClient(config)

def get_image_ocid():
    images = oci.core.ComputeClient(config).list_images(
        compartment_id            = os.environ["OCI_COMPARTMENT_OCID"],
        operating_system          = "Canonical Ubuntu",
        operating_system_version  = "22.04",
        shape                     = "VM.Standard.A1.Flex",
        sort_by                   = "TIMECREATED",
        sort_order                = "DESC",
    ).data
    if not images:
        print("No Ubuntu 22.04 ARM image found!")
        sys.exit(1)
    print(f"Using image: {images[0].display_name} ({images[0].id})")
    return images[0].id

def try_create(ad, image_ocid):
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain = ad,
        compartment_id      = os.environ["OCI_COMPARTMENT_OCID"],
        display_name        = "free-arm-instance",
        shape               = "VM.Standard.A1.Flex",
        shape_config        = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=4, memory_in_gbs=24,
        ),
        source_details = oci.core.models.InstanceSourceViaImageDetails(
            image_id              = image_ocid,
            source_type           = "image",
            boot_volume_size_in_gbs = 200,   # ← hier anpassen (47-200)
        ),
        create_vnic_details = oci.core.models.CreateVnicDetails(
            subnet_id        = os.environ["OCI_SUBNET_OCID"],
            assign_public_ip = True,
        ),
        metadata={"ssh_authorized_keys": SSH_PUB_KEY},
    )
    return compute.launch_instance(details)

image_ocid = get_image_ocid()

for ad in ADS:
    try:
        print(f"[{datetime.datetime.utcnow()}] Trying {ad}...")
        resp = try_create(ad, image_ocid)
        print(f"SUCCESS in {ad}: {resp.data.id}")
        sys.exit(0)
    except oci.exceptions.ServiceError as e:
        if "Out of host capacity" in str(e.message):
            print(f"  → Out of capacity in {ad}, trying next...")
        else:
            print(f"  → Unexpected error in {ad}: {e}")
            sys.exit(1)

print("All ADs out of capacity — will retry next run.")
sys.exit(0)