import oci, os, sys, datetime, urllib.request, time, json

config = {
    "user":        os.environ["OCI_USER_OCID"],
    "tenancy":     os.environ["OCI_TENANCY_OCID"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "region":      os.environ["OCI_REGION"],
    "key_file":    os.path.expanduser("~/.oci/key.pem"),
}

SSH_PUB_KEY = os.environ["OCI_SSH_PUBLIC_KEY"]
COMPARTMENT = os.environ["OCI_COMPARTMENT_OCID"]
SUBNET      = os.environ["OCI_SUBNET_OCID"]
NTFY_TOPIC  = "oracle-teynz-7x92"  # ← hier deinen eigenen Namen einsetzen

ADS = [
    "zsnG:EU-FRANKFURT-1-AD-1",
    "zsnG:EU-FRANKFURT-1-AD-2",
    "zsnG:EU-FRANKFURT-1-AD-3",
]

compute = oci.core.ComputeClient(config)
network = oci.core.VirtualNetworkClient(config)


def disable_workflow():
    """Deaktiviert sowohl GitHub Workflow als auch cron-job.org Job."""
    _disable_github_workflow()
    _disable_cronjob_org()


def _disable_github_workflow():
    token    = os.environ.get("GITHUB_TOKEN")
    repo     = os.environ.get("GITHUB_REPOSITORY")
    workflow = os.environ.get("GITHUB_WORKFLOW_REF", "").split("@")[0].split("/")[-1]
    if not all([token, repo, workflow]):
        print("GitHub env vars nicht verfügbar — Workflow manuell deaktivieren!")
        return
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/disable"
    req = urllib.request.Request(url, method="PUT", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    try:
        urllib.request.urlopen(req, timeout=10)
        print("GitHub Workflow automatisch deaktiviert.")
    except Exception as e:
        print(f"GitHub Workflow deaktivieren fehlgeschlagen: {e}")


def _disable_cronjob_org():
    api_key = os.environ.get("CRONJOB_API_KEY")
    job_id  = os.environ.get("CRONJOB_JOB_ID")
    if not all([api_key, job_id]):
        print("cron-job.org Credentials fehlen — manuell deaktivieren!")
        return
    url = f"https://api.cron-job.org/jobs/{job_id}"
    body = json.dumps({"job": {"enabled": False}}).encode()
    req = urllib.request.Request(url, data=body, method="PATCH", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    try:
        urllib.request.urlopen(req, timeout=10)
        print("cron-job.org Job automatisch deaktiviert.")
    except Exception as e:
        print(f"cron-job.org deaktivieren fehlgeschlagen: {e}")


def instance_already_exists():
    instances = oci.pagination.list_call_get_all_results(
        compute.list_instances,
        compartment_id=COMPARTMENT,
    ).data
    for inst in instances:
        if inst.shape == "VM.Standard.A1.Flex" and inst.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            print(f"Instanz existiert bereits: {inst.display_name} ({inst.id}) — Status: {inst.lifecycle_state}")
            return True
    return False


def get_image_ocid():
    images = compute.list_images(
        compartment_id           = COMPARTMENT,
        operating_system         = "Canonical Ubuntu",
        operating_system_version = "24.04 Minimal",
        shape                    = "VM.Standard.A1.Flex",
        sort_by                  = "TIMECREATED",
        sort_order               = "DESC",
    ).data
    if not images:
        print("Kein Ubuntu 24.04 ARM Image gefunden!")
        sys.exit(1)
    print(f"Using image: {images[0].display_name}")
    return images[0].id


def try_create(ad, image_ocid):
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain = ad,
        compartment_id      = COMPARTMENT,
        display_name        = "free-arm-instance",
        shape               = "VM.Standard.A1.Flex",
        shape_config        = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=4, memory_in_gbs=24,
        ),
        source_details = oci.core.models.InstanceSourceViaImageDetails(
            image_id                = image_ocid,
            source_type             = "image",
            boot_volume_size_in_gbs = 200,
        ),
        create_vnic_details = oci.core.models.CreateVnicDetails(
            subnet_id        = SUBNET,
            assign_public_ip = True,
        ),
        metadata={"ssh_authorized_keys": SSH_PUB_KEY},
    )
    return compute.launch_instance(details)


def get_public_ip(instance_id):
    for _ in range(12):  # max 60s warten
        try:
            vnic_attachments = compute.list_vnic_attachments(
                compartment_id=COMPARTMENT,
                instance_id=instance_id,
            ).data
            if vnic_attachments:
                vnic = network.get_vnic(vnic_attachments[0].vnic_id).data
                if vnic.public_ip:
                    return vnic.public_ip
        except Exception as e:
            print(f"  VNIC noch nicht bereit: {e}")
        time.sleep(5)
    return None


def send_notification(ip):
    msg = f"Oracle Instanz erstellt! SSH: ubuntu@{ip}" if ip else "Oracle Instanz erstellt! IP noch nicht verfügbar — OCI Console checken."
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=msg.encode(),
        method="POST",
        headers={"Title": "Oracle Cloud Instance bereit!"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print("Benachrichtigung gesendet.")
    except Exception as e:
        print(f"Benachrichtigung fehlgeschlagen: {e}")


# === Hauptlogik ===

if instance_already_exists():
    print("Nichts zu tun — Workflow wird deaktiviert.")
    disable_workflow()
    sys.exit(0)

image_ocid = get_image_ocid()

for ad in ADS:
    try:
        print(f"[{datetime.datetime.now(datetime.UTC)}] Versuche {ad}...")
        resp = try_create(ad, image_ocid)
        print(f"ERFOLG in {ad}: {resp.data.id}")
        ip = get_public_ip(resp.data.id)
        if ip:
            print(f"Public IP: {ip}")
            print(f"SSH: ssh ubuntu@{ip}")
        send_notification(ip)
        disable_workflow()
        sys.exit(0)
    except oci.exceptions.ServiceError as e:
        msg = str(e.message or "")
        status = e.status
        if "Out of host capacity" in msg or status == 500:
            print(f"  → {ad}: keine Capacity (status {status})")
            continue
        if status == 429:
            print(f"  → Rate Limit erreicht, beende sauber.")
            sys.exit(0)
        print(f"  → Fataler Fehler in {ad}: status={status}, msg={msg}")
        sys.exit(1)

print("Alle ADs ohne Capacity — nächster Run in 5 Minuten.")
sys.exit(0)