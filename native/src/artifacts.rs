//! Unlinked private files bound artifact retention to server fd lifetime,
//! including crashes. Store <=64 MiB plus four <=16 MiB staging/read leases.
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, VecDeque},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{FileExt, OpenOptionsExt},
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio::sync::Semaphore;
const ITEM_LIMIT: u64 = 16 * 1024 * 1024;
const TOTAL_LIMIT: u64 = 64 * 1024 * 1024;
const COUNT_LIMIT: usize = 64;
const RANGE_LIMIT: u64 = 65536;
const FORWARD_LIMIT: u64 = 512 * 1024;
struct Entry {
    id: String,
    owner: String,
    file: File,
    size: u64,
    format: String,
    sha256: String,
    #[cfg(test)]
    verification_scans: std::sync::atomic::AtomicUsize,
}
struct UploadData {
    file: File,
    size: u64,
    hash: Sha256,
}
struct Upload {
    owner: String,
    run: String,
    format: String,
    alive: AtomicBool,
    data: Mutex<UploadData>,
}
impl Entry {
    fn reference(&self) -> Value {
        json!({"id":self.id,"size":self.size,"format":self.format,"sha256":self.sha256,"lifetime":"server","eviction":"oldest-first"})
    }
}
struct Reader<'a> {
    file: &'a File,
    position: u64,
    remaining: u64,
}
impl Read for Reader<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        let length = buffer.len().min(self.remaining as usize);
        if length == 0 {
            return Ok(0);
        }
        let count = self.file.read_at(&mut buffer[..length], self.position)?;
        self.position += count as u64;
        self.remaining -= count as u64;
        Ok(count)
    }
}
pub struct Artifacts {
    entries: Mutex<VecDeque<Arc<Entry>>>,
    owners: Mutex<HashMap<String, Arc<AtomicBool>>>,
    runs: Mutex<HashMap<(String, String), Arc<AtomicBool>>>,
    uploads: Mutex<HashMap<String, Arc<Upload>>>,
    jobs: Arc<Semaphore>,
}
fn field<'a>(params: &'a Value, key: &str) -> Result<&'a str, String> {
    params
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("Artifact {key} must be a string"))
}
fn private_file(path: &Path) -> Result<File, String> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .map_err(|_| "Artifact file cannot be created".into())
}
fn regular_input(path: &Path) -> Result<File, String> {
    // Nonblocking open followed by fstat closes the FIFO pathname-replacement race.
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| "Artifact input cannot be opened")?;
    if !file
        .metadata()
        .map_err(|_| "Artifact input metadata unavailable")?
        .is_file()
    {
        return Err("Artifact input must be a regular file".into());
    }
    Ok(file)
}
fn digest(mut input: impl Read) -> Result<(String, u64), String> {
    let mut hash = Sha256::new();
    let mut size = 0;
    let mut buffer = [0_u8; 8192];
    loop {
        let count = input
            .read(&mut buffer)
            .map_err(|_| "Artifact read failed")?;
        if count == 0 {
            break;
        }
        size += count as u64;
        if size > ITEM_LIMIT {
            return Err("Artifact exceeds 16 MiB".into());
        }
        hash.update(&buffer[..count]);
    }
    Ok((format!("{:x}", hash.finalize()), size))
}
impl Artifacts {
    pub fn new() -> Result<Self, String> {
        Ok(Self {
            entries: Mutex::new(VecDeque::new()),
            owners: Mutex::new(HashMap::new()),
            runs: Mutex::new(HashMap::new()),
            uploads: Mutex::new(HashMap::new()),
            jobs: Arc::new(Semaphore::new(4)),
        })
    }
    pub fn register_owner(&self, owner: &str) -> Result<(), String> {
        let mut owners = self
            .owners
            .lock()
            .map_err(|_| "Artifact owner registry unavailable")?;
        if owners.len() >= 256 && !owners.contains_key(owner) {
            return Err("Artifact active owner capacity exhausted".into());
        }
        owners
            .entry(owner.into())
            .or_insert_with(|| Arc::new(AtomicBool::new(true)));
        Ok(())
    }
    pub fn register_run(&self, owner: &str, run: &str) -> Result<(), String> {
        let _commit = self
            .entries
            .lock()
            .map_err(|_| "Artifact store unavailable")?;
        if !self
            .owners
            .lock()
            .map_err(|_| "Artifact owner registry unavailable")?
            .contains_key(owner)
        {
            return Err("Artifact owner session is closed or unregistered".into());
        }
        let mut runs = self
            .runs
            .lock()
            .map_err(|_| "Artifact run registry unavailable")?;
        if runs.len() >= 256 {
            return Err("Artifact active run capacity exhausted".into());
        }
        runs.entry((owner.into(), run.into()))
            .or_insert_with(|| Arc::new(AtomicBool::new(true)));
        Ok(())
    }
    pub fn abort_unfinished_uploads(&self, owner: &str, run: &str) -> Result<usize, String> {
        let _commit = self
            .entries
            .lock()
            .map_err(|_| "Artifact store unavailable")?;
        if let Some(guard) = self
            .runs
            .lock()
            .map_err(|_| "Artifact run registry unavailable")?
            .remove(&(owner.into(), run.into()))
        {
            guard.store(false, Ordering::Release);
        }
        let mut uploads = self
            .uploads
            .lock()
            .map_err(|_| "Artifact upload registry unavailable")?;
        let before = uploads.len();
        uploads.retain(|_, upload| {
            if upload.owner == owner && upload.run == run {
                upload.alive.store(false, Ordering::Release);
                false
            } else {
                true
            }
        });
        Ok(before - uploads.len())
    }
    pub async fn dispatch(
        self: &Arc<Self>,
        owner: &str,
        op: &str,
        params: Value,
    ) -> Result<Value, String> {
        let permit = self
            .jobs
            .clone()
            .try_acquire_owned()
            .map_err(|_| "Artifact I/O capacity exhausted; retry after active jobs complete")?;
        let store = self.clone();
        let owner_guard = self
            .owners
            .lock()
            .map_err(|_| "Artifact owner registry unavailable")?
            .get(owner)
            .cloned()
            .ok_or("Artifact owner session is closed or unregistered")?;
        let run_guard = if let Some(run) = params.get("_run_id").and_then(Value::as_str) {
            Some(
                self.runs
                    .lock()
                    .map_err(|_| "Artifact run registry unavailable")?
                    .get(&(owner.into(), run.into()))
                    .cloned()
                    .ok_or("Artifact originating run is finished or cancelled")?,
            )
        } else {
            None
        };
        let owner = owner.to_string();
        let op = op.to_string();
        tokio::task::spawn_blocking(move || {
            let _permit = permit;
            store.dispatch_sync(&owner, &op, &params, &owner_guard, run_guard.as_ref())
        })
        .await
        .map_err(|_| "Artifact I/O task failed")?
    }
    pub async fn remove_owner(&self, owner: &str) -> Result<usize, String> {
        let mut entries = self
            .entries
            .lock()
            .map_err(|_| "Artifact store unavailable")?;
        let before = entries.len();
        if let Some(guard) = self
            .owners
            .lock()
            .map_err(|_| "Artifact owner registry unavailable")?
            .remove(owner)
        {
            guard.store(false, Ordering::Release);
        }
        self.runs
            .lock()
            .map_err(|_| "Artifact run registry unavailable")?
            .retain(|(id, _), guard| {
                if id == owner {
                    guard.store(false, Ordering::Release);
                    false
                } else {
                    true
                }
            });
        self.uploads
            .lock()
            .map_err(|_| "Artifact upload registry unavailable")?
            .retain(|_, upload| {
                if upload.owner == owner {
                    upload.alive.store(false, Ordering::Release);
                    false
                } else {
                    true
                }
            });
        entries.retain(|entry| entry.owner != owner);
        Ok(before - entries.len())
    }
    pub async fn forward_payload(
        self: &Arc<Self>,
        owner: &str,
        id: &str,
        format: &str,
    ) -> Result<Value, String> {
        let permit = self
            .jobs
            .clone()
            .try_acquire_owned()
            .map_err(|_| "Artifact I/O capacity exhausted")?;
        let store = self.clone();
        let owner = owner.to_string();
        let id = id.to_string();
        let format = format.to_string();
        tokio::task::spawn_blocking(move || {
            let _permit=permit; let entry=store.lookup(&owner,&id)?;
            if entry.size>FORWARD_LIMIT { return Err("Artifact forwarding limit is 512 KiB; save the file or explicitly create a smaller artifact".into()); }
            store.verify(&entry)?; let mut bytes=Vec::with_capacity(entry.size as usize); store.reader(&entry).read_to_end(&mut bytes).map_err(|_|"Artifact read failed")?;
            match format.as_str() { "json"=>serde_json::from_slice(&bytes).map_err(|_|"Artifact is not valid bounded JSON".into()), "text"=>String::from_utf8(bytes).map(Value::String).map_err(|_|"Artifact is not UTF-8 text".into()), "base64"=>Ok(Value::String(STANDARD.encode(bytes))), _=>Err("Forward format must be json, text or explicit base64".into()) }
        }).await.map_err(|_|"Artifact forwarding task failed")?
    }
    fn lookup(&self, owner: &str, id: &str) -> Result<Arc<Entry>, String> {
        self.entries
            .lock()
            .map_err(|_| "Artifact store unavailable")?
            .iter()
            .find(|entry| entry.id == id && entry.owner == owner)
            .cloned()
            .ok_or_else(|| {
                "Artifact unknown, expired, evicted or belongs to another session".into()
            })
    }
    fn reader<'a>(&self, entry: &'a Entry) -> Reader<'a> {
        Reader {
            file: &entry.file,
            position: 0,
            remaining: entry.size,
        }
    }
    fn verify(&self, entry: &Entry) -> Result<(), String> {
        #[cfg(test)]
        entry.verification_scans.fetch_add(1, Ordering::Relaxed);
        let (hash, size) = digest(self.reader(entry))?;
        if hash != entry.sha256
            || size != entry.size
            || entry
                .file
                .metadata()
                .map_err(|_| "Artifact metadata unavailable")?
                .len()
                != entry.size
        {
            return Err("Artifact content failed SHA256 verification".into());
        }
        Ok(())
    }
    fn dispatch_sync(
        &self,
        owner: &str,
        op: &str,
        params: &Value,
        owner_guard: &Arc<AtomicBool>,
        run_guard: Option<&Arc<AtomicBool>>,
    ) -> Result<Value, String> {
        match op {
            "artifact.begin" | "artifact.append" | "artifact.commit" | "artifact.abort" => self
                .upload(
                    owner,
                    op,
                    params,
                    owner_guard,
                    run_guard.ok_or("Artifact upload needs an active originating run")?,
                ),
            "artifact.create" | "create" => self.create(owner, params, owner_guard, run_guard),
            "artifact.read" | "read" => {
                let entry = self.lookup(owner, field(params, "id")?)?;
                let offset = params.get("offset").map_or(Ok(0), |v| {
                    v.as_u64().ok_or("Artifact offset must be unsigned integer")
                })?;
                let length = params.get("length").map_or(Ok(RANGE_LIMIT), |v| {
                    v.as_u64().ok_or("Artifact length must be unsigned integer")
                })?;
                if length > RANGE_LIMIT || offset > entry.size {
                    return Err(
                        "Artifact range must fit file and length must be <= 65536 bytes".into(),
                    );
                }
                // Published entries have private, unlinked descriptors and no
                // writer after commit. Their SHA is established at publication;
                // range reads must not rescan the entire item per page.
                let length = length.min(entry.size - offset);
                let mut reader = Reader {
                    file: &entry.file,
                    position: offset,
                    remaining: length,
                };
                let mut bytes = vec![0; length as usize];
                reader
                    .read_exact(&mut bytes)
                    .map_err(|_| "Artifact range read failed")?;
                let encoding = params
                    .get("encoding")
                    .and_then(Value::as_str)
                    .unwrap_or("text");
                let content = match encoding {
                    "text" => String::from_utf8(bytes).map_err(
                        |_| "Range is not complete UTF-8; adjust boundaries or request hex/base64",
                    )?,
                    "base64" => STANDARD.encode(bytes),
                    "hex" => bytes.iter().map(|byte| format!("{byte:02x}")).collect(),
                    _ => {
                        return Err("Artifact encoding must be text, hex or explicit base64".into());
                    }
                };
                Ok(
                    json!({"artifact":entry.reference(),"offset":offset,"length":length,"encoding":encoding,"content":content,"has_more":offset+length<entry.size}),
                )
            }
            "artifact.save" | "save" => {
                let entry = self.lookup(owner, field(params, "id")?)?;
                let destination = Path::new(field(params, "path")?);
                if !destination.is_absolute() {
                    return Err("Artifact destination must be absolute".into());
                }
                let temporary = destination
                    .parent()
                    .ok_or("Artifact destination has no parent")?
                    .join(format!(".repl-artifact-save-{}", uuid::Uuid::new_v4()));
                let result = (|| {
                    self.verify(&entry)?;
                    let mut output = private_file(&temporary)?;
                    std::io::copy(&mut self.reader(&entry), &mut output)
                        .map_err(|_| "Artifact save failed")?;
                    output.sync_all().map_err(|_| "Artifact save sync failed")?;
                    if params
                        .get("overwrite")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                    {
                        fs::rename(&temporary, destination)
                            .map_err(|_| "Artifact atomic replace failed")?;
                    } else {
                        fs::hard_link(&temporary,destination).map_err(|_|"Artifact destination exists or cannot be linked; overwrite requires explicit true")?;
                    }
                    Ok(json!({"artifact":entry.reference(),"saved":true,"path":destination}))
                })();
                let _ = fs::remove_file(&temporary);
                result
            }
            "artifact.drop" | "drop" => {
                let id = field(params, "id")?;
                let mut entries = self
                    .entries
                    .lock()
                    .map_err(|_| "Artifact store unavailable")?;
                let position = entries
                    .iter()
                    .position(|entry| entry.id == id && entry.owner == owner)
                    .ok_or("Artifact unknown, expired, evicted or belongs to another session")?;
                entries.remove(position);
                Ok(json!({"id":id,"deleted":true}))
            }
            _ => Err("Unknown artifact operation".into()),
        }
    }
    fn create(
        &self,
        owner: &str,
        params: &Value,
        owner_guard: &Arc<AtomicBool>,
        run_guard: Option<&Arc<AtomicBool>>,
    ) -> Result<Value, String> {
        let source = Path::new(field(params, "path")?);
        let format = field(params, "format")?;
        if !source.is_absolute() || !matches!(format, "json" | "text" | "binary") {
            return Err("Artifact needs absolute path and format json/text/binary".into());
        }
        let mut input = regular_input(source)?;
        let temporary =
            std::env::temp_dir().join(format!("repl-mcp-artifact-{}", uuid::Uuid::new_v4()));
        let mut output = private_file(&temporary)?;
        // Unlink before writing: all retained data belongs to descriptors. Hard
        // process death closes those descriptors without filesystem cache leaks.
        if fs::remove_file(&temporary).is_err() {
            drop(output);
            let _ = fs::remove_file(temporary);
            return Err("Artifact temporary unlink failed".into());
        }
        let mut hash = Sha256::new();
        let mut size = 0;
        let mut buffer = [0_u8; 8192];
        loop {
            let count = input
                .read(&mut buffer)
                .map_err(|_| "Artifact input read failed")?;
            if count == 0 {
                break;
            }
            size += count as u64;
            if size > ITEM_LIMIT {
                return Err("Artifact exceeds 16 MiB".into());
            }
            output
                .write_all(&buffer[..count])
                .map_err(|_| "Artifact store write failed")?;
            hash.update(&buffer[..count]);
        }
        output.flush().map_err(|_| "Artifact store flush failed")?;
        if format != "binary" {
            let mut bytes = Vec::with_capacity(size as usize);
            Reader {
                file: &output,
                position: 0,
                remaining: size,
            }
            .read_to_end(&mut bytes)
            .map_err(|_| "Artifact validation read failed")?;
            std::str::from_utf8(&bytes).map_err(|_| "Text/JSON artifact must be UTF-8")?;
            if format == "json" {
                let _: Value = serde_json::from_slice(&bytes)
                    .map_err(|_| "Artifact JSON invalid or exceeds parser nesting limit")?;
            }
        }
        let entry = Arc::new(Entry {
            id: uuid::Uuid::new_v4().to_string(),
            owner: owner.into(),
            file: output,
            size,
            format: format.into(),
            sha256: format!("{:x}", hash.finalize()),
            #[cfg(test)]
            verification_scans: std::sync::atomic::AtomicUsize::new(0),
        });
        let mut entries = self
            .entries
            .lock()
            .map_err(|_| "Artifact store unavailable")?;
        if !owner_guard.load(Ordering::Acquire)
            || run_guard.is_some_and(|guard| !guard.load(Ordering::Acquire))
        {
            return Err("Artifact owner session closed before import completed".into());
        }
        // Only successful imports commit evictions. Failed input cannot erase
        // prior references. The four actual blocking jobs bound transient staging.
        while entries.len() >= COUNT_LIMIT
            || entries.iter().map(|entry| entry.size).sum::<u64>() + size > TOTAL_LIMIT
        {
            entries.pop_front();
        }
        let reference = entry.reference();
        entries.push_back(entry);
        Ok(reference)
    }
    fn upload(
        &self,
        owner: &str,
        op: &str,
        params: &Value,
        owner_guard: &Arc<AtomicBool>,
        run_guard: &Arc<AtomicBool>,
    ) -> Result<Value, String> {
        let run = field(params, "_run_id")?;
        if op == "artifact.begin" {
            let format = field(params, "format")?;
            if !matches!(format, "json" | "text" | "binary") {
                return Err("Artifact format must be json/text/binary".into());
            }
            let temporary =
                std::env::temp_dir().join(format!("repl-mcp-artifact-{}", uuid::Uuid::new_v4()));
            let file = private_file(&temporary)?;
            if fs::remove_file(&temporary).is_err() {
                drop(file);
                let _ = fs::remove_file(temporary);
                return Err("Artifact temporary unlink failed".into());
            }
            let _commit = self
                .entries
                .lock()
                .map_err(|_| "Artifact store unavailable")?;
            if !owner_guard.load(Ordering::Acquire) || !run_guard.load(Ordering::Acquire) {
                return Err("Artifact owner/run closed before upload began".into());
            }
            let mut uploads = self
                .uploads
                .lock()
                .map_err(|_| "Artifact upload registry unavailable")?;
            if uploads.len() >= 4 {
                return Err("At most four unfinished artifact uploads are allowed".into());
            }
            let id = uuid::Uuid::new_v4().to_string();
            uploads.insert(
                id.clone(),
                Arc::new(Upload {
                    owner: owner.into(),
                    run: run.into(),
                    format: format.into(),
                    alive: AtomicBool::new(true),
                    data: Mutex::new(UploadData {
                        file,
                        size: 0,
                        hash: Sha256::new(),
                    }),
                }),
            );
            return Ok(json!({"upload_id":id,"chunk_bytes":32768,"max_bytes":ITEM_LIMIT}));
        }
        let id = field(params, "upload_id")?;
        let upload = self
            .uploads
            .lock()
            .map_err(|_| "Artifact upload registry unavailable")?
            .get(id)
            .filter(|upload| upload.owner == owner && upload.run == run)
            .cloned()
            .ok_or("Artifact upload unknown, finished or cancelled")?;
        if op == "artifact.abort" {
            let _commit = self
                .entries
                .lock()
                .map_err(|_| "Artifact store unavailable")?;
            upload.alive.store(false, Ordering::Release);
            self.uploads
                .lock()
                .map_err(|_| "Artifact upload registry unavailable")?
                .remove(id);
            return Ok(json!({"upload_id":id,"aborted":true}));
        }
        let mut data = upload
            .data
            .lock()
            .map_err(|_| "Artifact upload unavailable")?;
        if !upload.alive.load(Ordering::Acquire)
            || !owner_guard.load(Ordering::Acquire)
            || !run_guard.load(Ordering::Acquire)
        {
            return Err("Artifact upload owner/run is cancelled".into());
        }
        if op == "artifact.append" {
            let encoded = field(params, "data")?;
            if encoded.len() > 43692 {
                return Err("Artifact upload chunk exceeds 32 KiB".into());
            }
            let bytes = STANDARD
                .decode(encoded)
                .map_err(|_| "Artifact upload chunk is not valid base64")?;
            if bytes.len() > 32768 {
                return Err("Artifact upload chunk exceeds 32 KiB".into());
            }
            let offset = params
                .get("offset")
                .and_then(Value::as_u64)
                .ok_or("Artifact upload offset must be unsigned integer")?;
            if offset != data.size {
                return Err("Artifact upload offset mismatch; do not replay an append".into());
            }
            if data.size + bytes.len() as u64 > ITEM_LIMIT {
                return Err("Artifact exceeds 16 MiB".into());
            }
            data.file
                .write_all(&bytes)
                .map_err(|_| "Artifact upload write failed")?;
            data.hash.update(&bytes);
            data.size += bytes.len() as u64;
            return Ok(json!({"upload_id":id,"size":data.size}));
        }
        if op != "artifact.commit" {
            return Err("Unknown artifact upload operation".into());
        }
        data.file
            .flush()
            .map_err(|_| "Artifact upload flush failed")?;
        if upload.format != "binary" {
            let mut bytes = Vec::with_capacity(data.size as usize);
            Reader {
                file: &data.file,
                position: 0,
                remaining: data.size,
            }
            .read_to_end(&mut bytes)
            .map_err(|_| "Artifact validation read failed")?;
            std::str::from_utf8(&bytes).map_err(|_| "Text/JSON artifact must be UTF-8")?;
            if upload.format == "json" {
                let _: Value = serde_json::from_slice(&bytes)
                    .map_err(|_| "Artifact JSON invalid or exceeds parser nesting limit")?;
            }
        }
        let entry = Arc::new(Entry {
            id: uuid::Uuid::new_v4().to_string(),
            owner: owner.into(),
            file: data
                .file
                .try_clone()
                .map_err(|_| "Artifact descriptor duplication failed")?,
            size: data.size,
            format: upload.format.clone(),
            sha256: format!("{:x}", data.hash.clone().finalize()),
            #[cfg(test)]
            verification_scans: std::sync::atomic::AtomicUsize::new(0),
        });
        let mut entries = self
            .entries
            .lock()
            .map_err(|_| "Artifact store unavailable")?;
        if !upload.alive.load(Ordering::Acquire)
            || !owner_guard.load(Ordering::Acquire)
            || !run_guard.load(Ordering::Acquire)
        {
            return Err("Artifact owner/run closed before upload committed".into());
        }
        while entries.len() >= COUNT_LIMIT
            || entries.iter().map(|entry| entry.size).sum::<u64>() + entry.size > TOTAL_LIMIT
        {
            entries.pop_front();
        }
        let reference = entry.reference();
        entries.push_back(entry);
        upload.alive.store(false, Ordering::Release);
        self.uploads
            .lock()
            .map_err(|_| "Artifact upload registry unavailable")?
            .remove(id);
        Ok(reference)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn paginated_ranges_skip_full_scans_but_forward_verifies() {
        let store = Arc::new(Artifacts::new().unwrap());
        store.register_owner("a").unwrap();
        let bytes = vec![b'x'; 4 * RANGE_LIMIT as usize];
        let path = input(&bytes);
        let reference = store
            .dispatch("a", "create", json!({"path":path,"format":"text"}))
            .await
            .unwrap();
        let id = reference["id"].as_str().unwrap();
        let entry = store.lookup("a", id).unwrap();
        for page in 0..4 {
            let result = store
                .dispatch(
                    "a",
                    "read",
                    json!({"id":id,"offset":page * RANGE_LIMIT,"length":RANGE_LIMIT}),
                )
                .await
                .unwrap();
            assert_eq!(result["length"], RANGE_LIMIT);
        }
        assert_eq!(entry.verification_scans.load(Ordering::Relaxed), 0);
        store.forward_payload("a", id, "text").await.unwrap();
        assert_eq!(entry.verification_scans.load(Ordering::Relaxed), 1);
        fs::remove_file(path).unwrap();
    }
    #[tokio::test]
    async fn uploads_commit_survive_run_cleanup_and_incomplete_uploads_close() {
        let store = Arc::new(Artifacts::new().unwrap());
        store.register_owner("a").unwrap();
        store.register_run("a", "run-1").unwrap();
        let begin = store
            .dispatch(
                "a",
                "artifact.begin",
                json!({"_run_id":"run-1","format":"json"}),
            )
            .await
            .unwrap();
        let upload = begin["upload_id"].as_str().unwrap();
        let bytes = b"{\"ok\":true}";
        store.dispatch("a","artifact.append",json!({"_run_id":"run-1","upload_id":upload,"offset":0,"data":STANDARD.encode(bytes)})).await.unwrap();
        assert!(
            store
                .dispatch(
                    "a",
                    "artifact.append",
                    json!({"_run_id":"run-1","upload_id":upload,"offset":0,"data":"eA=="})
                )
                .await
                .is_err()
        );
        let reference = store
            .dispatch(
                "a",
                "artifact.commit",
                json!({"_run_id":"run-1","upload_id":upload}),
            )
            .await
            .unwrap();
        assert_eq!(reference["sha256"], format!("{:x}", Sha256::digest(bytes)));
        assert_eq!(store.abort_unfinished_uploads("a", "run-1").unwrap(), 0);
        assert_eq!(
            store
                .dispatch("a", "read", json!({"id":reference["id"]}))
                .await
                .unwrap()["content"],
            "{\"ok\":true}"
        );
        store.register_run("a", "run-2").unwrap();
        store
            .dispatch(
                "a",
                "artifact.begin",
                json!({"_run_id":"run-2","format":"binary"}),
            )
            .await
            .unwrap();
        assert_eq!(store.abort_unfinished_uploads("a", "run-2").unwrap(), 1);
        assert!(store.uploads.lock().unwrap().is_empty());
        assert!(store.runs.lock().unwrap().is_empty());
    }
    #[test]
    fn late_physical_begin_cannot_publish_after_run_drop() {
        let store = Artifacts::new().unwrap();
        store.register_owner("a").unwrap();
        store.register_run("a", "run").unwrap();
        let owner = store.owners.lock().unwrap().get("a").unwrap().clone();
        let run = store
            .runs
            .lock()
            .unwrap()
            .get(&("a".into(), "run".into()))
            .unwrap()
            .clone();
        store.abort_unfinished_uploads("a", "run").unwrap();
        let failure = store.dispatch_sync(
            "a",
            "artifact.begin",
            &json!({"_run_id":"run","format":"binary"}),
            &owner,
            Some(&run),
        );
        assert!(failure.is_err());
        assert!(store.uploads.lock().unwrap().is_empty());
    }
    #[tokio::test]
    async fn upload_slot_limit_and_owner_close_release_all_pending() {
        let store = Arc::new(Artifacts::new().unwrap());
        store.register_owner("a").unwrap();
        store.register_run("a", "run").unwrap();
        for _ in 0..4 {
            store
                .dispatch(
                    "a",
                    "artifact.begin",
                    json!({"_run_id":"run","format":"binary"}),
                )
                .await
                .unwrap();
        }
        assert!(
            store
                .dispatch(
                    "a",
                    "artifact.begin",
                    json!({"_run_id":"run","format":"binary"})
                )
                .await
                .is_err()
        );
        store.remove_owner("a").await.unwrap();
        assert!(store.uploads.lock().unwrap().is_empty());
        assert!(store.runs.lock().unwrap().is_empty());
        assert!(store.owners.lock().unwrap().is_empty());
    }
    fn input(bytes: &[u8]) -> std::path::PathBuf {
        let path =
            std::env::temp_dir().join(format!("repl-artifact-test-{}", uuid::Uuid::new_v4()));
        fs::write(&path, bytes).unwrap();
        path
    }
    #[tokio::test]
    async fn bounded_owner_hash_cleanup() {
        let store = Arc::new(Artifacts::new().unwrap());
        store.register_owner("a").unwrap();
        store.register_owner("b").unwrap();
        let input = input(b"hello");
        let r = store
            .dispatch("a", "create", json!({"path":input,"format":"text"}))
            .await
            .unwrap();
        fs::remove_file(input).unwrap();
        let id = r["id"].as_str().unwrap();
        assert!(store.dispatch("b", "read", json!({"id":id})).await.is_err());
        assert_eq!(
            store
                .dispatch("a", "read", json!({"id":id,"offset":1,"length":2}))
                .await
                .unwrap()["content"],
            "el"
        );
        assert!(
            store
                .dispatch("a", "read", json!({"id":id,"length":65537}))
                .await
                .is_err()
        );
        store
            .lookup("a", id)
            .unwrap()
            .file
            .write_at(b"xxxxx", 0)
            .unwrap();
        assert!(
            store
                .forward_payload("a", id, "text")
                .await
                .unwrap_err()
                .contains("SHA256")
        );
        assert_eq!(store.remove_owner("a").await.unwrap(), 1);
        assert!(store.lookup("a", id).is_err());
    }
    #[tokio::test]
    async fn failed_import_retains_oldest_and_fifo_returns() {
        let store = Arc::new(Artifacts::new().unwrap());
        store.register_owner("a").unwrap();
        let input = input(b"hello");
        let mut first = String::new();
        for index in 0..64 {
            let r = store
                .dispatch("a", "create", json!({"path":input,"format":"text"}))
                .await
                .unwrap();
            if index == 0 {
                first = r["id"].as_str().unwrap().into();
            }
        }
        assert!(
            store
                .dispatch("a", "create", json!({"path":input,"format":"json"}))
                .await
                .is_err()
        );
        assert!(store.lookup("a", &first).is_ok());
        let fifo = input.with_extension("fifo");
        let name = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(1),
            store.dispatch("a", "create", json!({"path":fifo,"format":"binary"})),
        )
        .await
        .unwrap();
        assert!(result.is_err());
        assert!(store.lookup("a", &first).is_ok());
        fs::remove_file(fifo).unwrap();
        fs::remove_file(input).unwrap();
    }
    #[tokio::test]
    async fn save_overwrite_forward_delete() {
        let store = Arc::new(Artifacts::new().unwrap());
        store.register_owner("a").unwrap();
        let input = input(b"{\"ok\":true}");
        let destination = input.with_extension("saved");
        let r = store
            .dispatch("a", "create", json!({"path":input,"format":"json"}))
            .await
            .unwrap();
        fs::remove_file(input).unwrap();
        let id = r["id"].as_str().unwrap();
        assert_eq!(
            store.forward_payload("a", id, "json").await.unwrap(),
            json!({"ok":true})
        );
        store
            .dispatch("a", "save", json!({"id":id,"path":destination}))
            .await
            .unwrap();
        assert!(
            store
                .dispatch("a", "save", json!({"id":id,"path":destination}))
                .await
                .is_err()
        );
        store
            .dispatch(
                "a",
                "save",
                json!({"id":id,"path":destination,"overwrite":true}),
            )
            .await
            .unwrap();
        fs::remove_file(destination).unwrap();
        store.dispatch("a", "drop", json!({"id":id})).await.unwrap();
        assert!(store.forward_payload("a", id, "json").await.is_err());
    }
}
