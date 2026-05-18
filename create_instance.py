import oci, os, sys, datetime, urllib.request, time, json, re

# === Env Validation (sofort beim Start, klare Fehler) ===
REQUIRED_ENV = [
    "OCI_USER_OCID", "OCI_TENANCY_OCID", "OCI_FINGERPRINT", "OCI_REGION",
    "OCI_COMPARTMENT_OCID", "OCI_SUBNET_OCID", "OCI_SSH_PUBLIC_KEY",
]
missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if missing:
    print(f"FATAL: Fehlende Env-Variablen: {missing}")
    sys.exit(1)

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
NTFY_TOPIC  = "oracle-teynz-7x92"

ADS = [
    "zsnG:EU-FRANKFURT-1-AD-1",
    "zsnG:EU-FRANKFURT-1-AD-2",
    "zsnG:EU-FRANKFURT-1-AD-3",
]

# Image-Präferenz (von oben nach unten, erstes Match gewinnt)
IMAGE_PATTERNS = [
    r"Canonical-Ubuntu-24\.04-Minimal-aarch64",
    r"Canonical-Ubuntu-24\.04-aarch64",
    r"Canonical-Ubuntu-22\.04-Minimal-aarch64",
    r"Canonical-Ubuntu-22\.04-aarch64",
]

compute = oci.core.ComputeClient(config)
network = oci.core.VirtualNetworkClient(config)


# === Self-Disable Funktionen ===
def disable_workflow():
    _disable_github_workflow()
    _disable_cronjob_org()


def _disable_github_workflow():
    token    = os.environ.get("GITHUB_TOKEN")
    repo     = os.environ.get("GITHUB_REPOSITORY")
    workflow = os.environ.get("GITHUB_WORKFLOW_REF", "").split("@")[0].split("/")[-1]
    if not all([token, repo, workflow]):
        print("GitHub env vars nicht verfügbar — manuell deaktivieren!")
        return
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/disable"
    req = urllib.request.Request(url, method="PUT", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    try:
        urllib.request.urlopen(req, timeout=10)
        print("GitHub Workflow deaktiviert.")
    except Exception as e:
        print(f"GitHub deaktivieren fehlgeschlagen: {e}")


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
        print("cron-job.org deaktiviert.")
    except Exception as e:
        print(f"cron-job.org deaktivieren fehlgeschlagen: {e}")


# === Helpers ===
def instance_already_exists():
    try:
        instances = oci.pagination.list_call_get_all_results(
            compute.list_instances,
            compartment_id=COMPARTMENT,
        ).data
    except Exception as e:
        print(f"FEHLER beim Listen der Instanzen: {e}")
        print("Sicherheitshalber abbrechen — kein Create-Versuch.")
        sys.exit(1)
    for inst in instances:
        if inst.shape == "VM.Standard.A1.Flex" and inst.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            print(f"Instanz existiert: {inst.display_name} ({inst.id}) — {inst.lifecycle_state}")
            return True
    return False


def get_image_ocid():
    """Findet das beste verfügbare Ubuntu ARM Image via Display-Name Matching."""
    try:
        images = oci.pagination.list_call_get_all_results(
            compute.list_images,
            compartment_id   = COMPARTMENT,
            operating_system = "Canonical Ubuntu",
            shape            = "VM.Standard.A1.Flex",
            sort_by          = "TIMECREATED",
            sort_order       = "DESC",
        ).data
    except Exception as e:
        print(f"FEHLER beim Listen der Images: {e}")
        sys.exit(1)

    if not images:
        print("Keine Canonical Ubuntu Images für A1.Flex gefunden!")
        sys.exit(1)

    print(f"  {len(images)} Ubuntu Images verfügbar")

    # Match nach Präferenz-Reihenfolge
    for pattern in IMAGE_PATTERNS:
        regex = re.compile(pattern)
        for img in images:
            if regex.search(img.display_name or ""):
                print(f"Using image: {img.display_name}  (pattern: {pattern})")
                return img.id

    # Fallback: erstes Ubuntu Image überhaupt
    print(f"WARN: Kein Pattern-Match — nehme neuestes: {images[0].display_name}")
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
            boot_volume_size_in_gbs = 150,
        ),
        create_vnic_details = oci.core.models.CreateVnicDetails(
            subnet_id        = SUBNET,
            assign_public_ip = True,
        ),
        metadata={"ssh_authorized_keys": SSH_PUB_KEY},
    )
    return compute.launch_instance(details)


def get_public_ip(instance_id):
    for _ in range(12):  # max 60s
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


def is_capacity_error(e):
    """Robuste Erkennung von Capacity-Fehlern."""
    msg = (e.message or "").lower()
    code = (getattr(e, "code", "") or "").lower()
    # Bekannte Capacity-Indikatoren
    if "out of host capacity" in msg:        return True
    if "out of capacity" in msg:             return True
    if "outofhostcapacity" in code:          return True
    if "internalerror" in code and e.status == 500: return True  # OCI tarnt Capacity oft so
    return False


# === Hauptlogik ===
if instance_already_exists():
    print("Nichts zu tun — beide Scheduler werden deaktiviert.")
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
        if is_capacity_error(e):
            print(f"  → {ad}: keine Capacity (status={e.status}, code={e.code})")
            continue
        if e.status == 429:
            print(f"  → Rate Limit, beende sauber.")
            sys.exit(0)
        if e.status in (401, 403):
            print(f"  → AUTH-FEHLER: status={e.status}, msg={e.message}")
            print("  → Credentials prüfen! Cron läuft weiter, aber das wird nie klappen.")
            sys.exit(1)
        print(f"  → Unerwarteter Fehler in {ad}: status={e.status}, code={e.code}, msg={e.message}")
        # Bei unbekannten Fehlern: weiterversuchen statt abbrechen
        continue
    except Exception as e:
        print(f"  → Unerwarteter Python-Fehler in {ad}: {type(e).__name__}: {e}")
        continue

print("Alle ADs durchprobiert — nächster Run via Scheduler.")
sys.exit(0)