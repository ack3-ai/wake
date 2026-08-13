use std::{
    collections::{hash_map::Entry, HashMap, VecDeque},
    fmt,
    iter::zip,
    mem,
    process,
};

use revm::{
    Database, DatabaseCommit, DatabaseRef, primitives::{Address, AddressMap, B256, KECCAK_EMPTY, U256 }, state::{Account, AccountInfo, Bytecode}
};

use bincode::{serialize_into, Options};
use std::fs::File;
use std::io::{self, Write};

use uuid::Uuid;

/// A position in the journal, together with the lineage that position belongs to.
///
/// An offset on its own cannot identify replay state. A snapshot revert discards
/// a suffix of the journal and the replacement branch reuses those offsets, so a
/// stale offset stops being out of range as soon as the new branch grows past it
/// and silently starts resolving against different state — the same offset, a
/// different history. `epoch` is what distinguishes the two.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JournalPoint {
    pub index: usize,
    pub epoch: u64,
    /// Newest mined block when this was recorded.
    ///
    /// The journal offset alone cannot place a point relative to a revert, because
    /// blocks move without the journal moving: empty blocks write no entries and a
    /// call never commits, so the first transaction after a few empty blocks lands
    /// on exactly the snapshot's offset and looks like shared history. The block
    /// number breaks that tie.
    ///
    /// A hash of the recorded block cannot: it proves one height is unchanged, and
    /// a replay reads the previous 256. These hashes are independent random values
    /// that commit to nothing, unlike a real chain where each commits to its parent,
    /// so one match says nothing about the ancestry actually being read.
    pub block_number: u64,
}

/// A journal position that can no longer be rolled back to.
///
/// The variants exist to keep the causes distinguishable in the user-facing
/// message: watching a trace disappear from a transaction is confusing enough
/// without the error conflating three different reasons.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JournalOutOfRange {
    /// Below the retained journal — the entries were dropped by history pruning.
    Pruned { requested: usize, base: usize },
    /// Above the journal tip — the entries were discarded by a snapshot revert.
    Reverted { requested: usize, tip: usize },
    /// In range, but recorded on a branch a snapshot revert discarded. The offset
    /// now belongs to the replacement branch and means something else.
    Diverged {
        requested: usize,
        requested_block: u64,
        epoch: u64,
        current_epoch: u64,
        shared_up_to: usize,
        shared_block: u64,
    },
    /// From a lineage whose divergence point has itself been compacted away, so
    /// how much it shares with the present is no longer knowable.
    Retired { epoch: u64, retired_before: u64 },
}

impl fmt::Display for JournalOutOfRange {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Pruned { requested, base } => write!(
                f,
                "journal index {requested} is outside the replayable range: history before \
                 index {base} has been pruned. Increase `testing.block_history` in the \
                 configuration (or set it to null to disable pruning) to keep older \
                 transactions replayable."
            ),
            Self::Reverted { requested, tip } => write!(
                f,
                "journal index {requested} is outside the replayable range after a snapshot \
                 revert: the journal now ends at index {tip}. Metadata of transactions from \
                 before the revert is still available, but they can no longer be re-executed."
            ),
            Self::Retired {
                epoch,
                retired_before,
            } => write!(
                f,
                "lineage {epoch} was retired (lineages before {retired_before} have been \
                 compacted away), so how much of its history the present still shares is no \
                 longer known. Metadata is still available, but it can no longer be re-executed."
            ),
            Self::Diverged {
                requested,
                requested_block,
                epoch,
                current_epoch,
                shared_up_to,
                shared_block,
            } => write!(
                f,
                "this was recorded on a branch discarded by a snapshot revert: journal index \
                 {requested} at block {requested_block} (lineage {epoch}, current lineage \
                 {current_epoch}), while that branch and the current one only share history up \
                 to journal index {shared_up_to} at block {shared_block}. Metadata is still \
                 available, but re-executing here would run against a different history and \
                 return a plausible, wrong result."
            ),
        }
    }
}

#[derive(Debug, Clone)]
pub enum JournalEntry {
    ContractChange(B256, Option<Bytecode>),
    AccountChange(Address, Option<DbAccount>),
    StorageChange(Address, HashMap<U256, Option<U256>>),
    StorageReplace(Address, Option<HashMap<U256, U256>>),
}

#[derive(serde::Serialize, serde::Deserialize)]
pub struct DiskCache {
    accounts: HashMap<Address, DbAccount>, // only accounts[0] from CacheDB is saved
    contracts: HashMap<B256, Bytecode>,
    storage: HashMap<Address, HashMap<U256, U256>>, // only storage[0] from CacheDB is saved
    block_hashes: HashMap<u64, B256>,
}

#[derive(Debug, Clone)]
pub struct CacheDB<ExtDB> {
    /// Account info where None means it is not existing. Not existing state is needed for Pre TANGERINE forks.
    /// `code` is always `None`, and bytecode can be found in `contracts`.
    /// Each item in the vector represents a snapshot.
    pub accounts: Vec<HashMap<Address, DbAccount>>,
    /// Tracks all contracts by their code hash.
    pub contracts: HashMap<B256, Bytecode>,
    pub storage: Vec<HashMap<Address, HashMap<U256, U256>>>,
    /// All cached block hashes from the [DatabaseRef].
    pub block_hashes: HashMap<u64, B256>,
    /// The underlying database ([DatabaseRef]) that is used to load data.
    ///
    /// Note: this is read-only, data is never written to this database.
    pub db: ExtDB,

    pub last_block_number: u64,
    /// Undo log. Entries hold *absolute prior values*, never deltas, and the
    /// current state is fully materialized in the `accounts`/`storage` layer
    /// stacks — so a prefix of this log can be dropped without folding anything
    /// into the entries that remain. See [`Self::compact_journal`].
    pub journal: VecDeque<JournalEntry>,
    /// Absolute index of `journal.front()`, i.e. how many entries have been
    /// compacted away. Every journal index stored outside this struct
    /// (`TransactionAbc::journal_index`, `Block::journal_index`,
    /// `snapshot_journal_indexes`) is absolute and stays valid across
    /// compaction; only indexing into `journal` needs this offset subtracted.
    pub journal_base: usize,
    /// Absolute journal indices at which snapshots were taken. Entries may sit
    /// *below* `journal_base` after compaction, and that is not corruption:
    /// snapshots deliberately do not pin the journal, because reverting one
    /// restores state by truncating the copy-on-write layer stack and never
    /// consults a journal entry.
    pub snapshot_journal_indexes: Vec<usize>,
    /// Which branch of history the journal is currently on. Bumped by every
    /// revert, even one that discards no journal entries, because re-mined blocks
    /// can give the same offsets different block contexts.
    pub journal_epoch: u64,
    /// How far each past lineage still agrees with the current one.
    ///
    /// `(from, shared_index, shared_block)` means every lineage in
    /// `from..next.from` shares history with the present through
    /// `(shared_index, shared_block)`. A point from lineage `e` remains replayable
    /// iff `(index, block_number) <= shared_up_to(e)`, compared lexicographically.
    ///
    /// Each entry is `(from_lineage, shared_journal_index, shared_block_number)`.
    /// The pair is compared lexicographically, so a point recorded at the snapshot's
    /// journal offset but on a *later* block is correctly placed after the cut.
    ///
    /// The shared bound is the *minimum* over all cuts since that lineage, not just
    /// the next one, so the entries are strictly increasing and a new cut pops every
    /// trailing entry it undercuts.
    journal_cuts: Vec<(u64, usize, u64)>,
    /// Lineages below this have had their cut records compacted away, so nothing
    /// from them can be replayed. Retiring rather than merely deleting matters
    /// because a later revert can lower `journal_base` again, which would
    /// otherwise resurrect points whose divergence point is no longer recorded.
    retired_epoch_before: u64,
}

impl<ExtDB: DatabaseRef> CacheDB<ExtDB> {
    pub fn new(db: ExtDB, last_block_number: u64) -> Self {
        let mut contracts = HashMap::new();
        contracts.insert(KECCAK_EMPTY, Bytecode::default());
        contracts.insert(B256::ZERO, Bytecode::default());
        Self {
            accounts: vec![HashMap::new(), HashMap::new()],
            contracts: contracts,
            storage: vec![HashMap::new(), HashMap::new()],
            block_hashes: HashMap::new(),
            db,
            last_block_number,
            journal: VecDeque::new(),
            journal_base: 0,
            snapshot_journal_indexes: vec![],
            journal_epoch: 0,
            journal_cuts: vec![],
            retired_epoch_before: 0,
        }
    }

    /// Absolute index one past the newest journal entry.
    #[inline]
    pub fn journal_index(&self) -> usize {
        self.journal_base + self.journal.len()
    }

    /// The current journal tip, tagged with the current lineage.
    #[inline]
    pub fn journal_point(&self) -> JournalPoint {
        JournalPoint {
            index: self.journal_index(),
            epoch: self.journal_epoch,
            block_number: self.last_block_number,
        }
    }

    /// How far lineage `epoch` still agrees with the present, or `None` if it is
    /// the present.
    fn shared_up_to(&self, epoch: u64) -> Option<(usize, u64)> {
        if epoch >= self.journal_epoch {
            return None;
        }
        // Entries partition the past lineages, so the applicable one is the last
        // whose `from` is at or below `epoch`.
        let position = self
            .journal_cuts
            .partition_point(|(from, _, _)| *from <= epoch);
        // A live past lineage always has an entry: the revert that ended it pushed
        // one, and `retire_cuts` bumps `retired_epoch_before` past any it removes,
        // so `rollback` rejects those before reaching here.
        debug_assert!(position > 0, "lineage {epoch} has no cut record");
        let (_, index, block) = *self.journal_cuts.get(position.wrapping_sub(1))?;
        Some((index, block))
    }

    /// Records that a revert truncated to `cut`, ending the current lineage.
    fn cut_lineage(&mut self, cut: usize, cut_block: u64) {
        // Clamp every past lineage to this cut as well: it is the minimum over all
        // cuts since, so anything it undercuts collapses into it.
        let mut from = self.journal_epoch;
        while let Some(&(earlier_from, shared, shared_block)) = self.journal_cuts.last() {
            if (shared, shared_block) >= (cut, cut_block) {
                from = earlier_from;
                self.journal_cuts.pop();
            } else {
                break;
            }
        }
        self.journal_cuts.push((from, cut, cut_block));
        self.journal_epoch += 1;
    }

    /// Drops every journal entry below `floor`, giving up the ability to roll
    /// back past it.
    ///
    /// A plain prefix drop is sound because entries store absolute prior values
    /// rather than deltas, so each retained entry is self-sufficient, and
    /// because the state itself lives in the `accounts`/`storage` layer stacks
    /// rather than in the log. Collapsing an *interior* span would be a
    /// different operation: it would have to merge per address/slot keeping the
    /// oldest prior value, and could not span a snapshot boundary at all, since
    /// [`Self::rollback_journal_entry`] derives its target layer from the
    /// entry's position relative to `snapshot_journal_indexes`.
    ///
    /// `floor` is clamped, so callers may pass a stale value. Returns the new base.
    pub fn compact_journal(&mut self, floor: usize) -> usize {
        let floor = floor.clamp(self.journal_base, self.journal_index());
        self.journal.drain(..floor - self.journal_base);
        self.journal_base = floor;
        self.retire_cuts();
        floor
    }

    /// Drops cut records the journal base has passed, retiring the lineages they
    /// governed.
    ///
    /// Without this the stack grows by one per revert whenever the cuts are
    /// *increasing* — snapshot at the advancing tip, write, revert, write — which
    /// `cut_lineage`'s collapse cannot fold, since it only merges cuts that undercut
    /// their predecessors. That is the ordinary fuzzing shape, so it grew unbounded.
    ///
    /// The records are retired rather than merely deleted because `revert_snapshot`
    /// can lower `journal_base` again; a deleted record would otherwise let its
    /// points look valid once more, and `shared_up_to` would index past the start of
    /// the stack looking for it.
    fn retire_cuts(&mut self) {
        let base = self.journal_base;
        let expired = self
            .journal_cuts
            .partition_point(|(_, shared, _)| *shared < base);
        if expired == 0 {
            return;
        }
        self.journal_cuts.drain(..expired);
        self.retired_epoch_before = self
            .journal_cuts
            .first()
            .map_or(self.journal_epoch, |(from, _, _)| *from);
        debug_assert!(
            self.journal_cuts.len() <= self.journal.len() + 1,
            "cut records must stay bounded by the retained journal"
        );
    }

    /// Drops cached block hashes that nothing can ask for any more.
    ///
    /// The reachable set is a `BLOCKHASH` horizon — 257 blocks, since both
    /// `block_hash` implementations answer for `[last - 256, last]` — around every
    /// block number that can be the tip. That is the current tip, plus one per
    /// `anchor`: a live snapshot can be reverted to, which lowers
    /// `last_block_number` and slides the horizon back onto numbers the chain has
    /// long since passed.
    ///
    /// The horizon is the floor, never the retained *metadata* window, because the
    /// two are unrelated: with `block_history = 8` the window is 9 blocks and the
    /// horizon is still 257. Getting this wrong is silent rather than loud —
    /// inside the horizon a missing entry is not a miss that refetches the right
    /// value; on a local chain the underlying database synthesizes one, so
    /// `BLOCKHASH` returns a plausible wrong hash instead of failing.
    pub fn prune_block_hashes(&mut self, anchors: &[u64]) {
        // A horizon is bounded at *both* ends, and the current tip is just another
        // one of them. Treating the tip as a floor only left every abandoned
        // branch's band stranded above it after a revert — unreadable, since both
        // `block_hash` implementations answer zero above the tip, and never
        // overwritten unless the chain climbs back through them.
        let horizon = |tip: u64, number: u64| number <= tip && number >= tip.saturating_sub(256);
        let tip = self.last_block_number;
        self.block_hashes.retain(|number, _| {
            horizon(tip, *number) || anchors.iter().any(|anchor| horizon(*anchor, *number))
        });
    }

    pub fn load_forked_state(&mut self, file_path: &str) -> Result<(), Box<dyn std::error::Error>> {
        let file = File::open(file_path).map_err(|e| Box::new(e) as Box<dyn std::error::Error>)?;
        let mut reader = io::BufReader::new(file);
        let disk_cache: DiskCache =
            bincode::options()
            .with_limit(10_000_000_000) // 10GB
            .with_fixint_encoding()
            .deserialize_from(&mut reader).map_err(|e| Box::new(e) as Box<dyn std::error::Error>)?;

        self.accounts[0] = disk_cache.accounts;
        self.contracts = disk_cache.contracts;
        self.storage[0] = disk_cache.storage;
        self.block_hashes = disk_cache.block_hashes;

        Ok(())
    }

    pub fn dump_forked_state(&mut self, file_path: &str) -> Result<(), Box<dyn std::error::Error>> {
        // Create a unique temp file name using PID and UUID
        let temp_path = format!("{}.{}.{}.tmp",
            file_path,
            process::id(),
            Uuid::new_v4()
        );

        let file = File::options()
            .write(true)
            .create_new(true)
            .open(&temp_path)
            .map_err(|e| Box::new(e) as Box<dyn std::error::Error>)?;

        let mut writer = io::BufWriter::new(file);

        let disk_cache = DiskCache {
            accounts: std::mem::take(&mut self.accounts[0]),
            contracts: std::mem::take(&mut self.contracts),
            storage: std::mem::take(&mut self.storage[0]),
            block_hashes: std::mem::take(&mut self.block_hashes),
        };

        // Write to temporary file
        serialize_into(&mut writer, &disk_cache)
            .map_err(|e| Box::new(e) as Box<dyn std::error::Error>)?;

        // Ensure all data is written to disk
        writer.flush().map_err(|e| Box::new(e) as Box<dyn std::error::Error>)?;

        // Atomically rename temp file to target file
        std::fs::rename(&temp_path, file_path)
            .map_err(|e| {
                // Clean up temp file if rename fails
                let _ = std::fs::remove_file(&temp_path);
                Box::new(e) as Box<dyn std::error::Error>
            })?;

        Ok(())
    }

    pub fn is_contract_forked(&self, address: &Address) -> Result<bool, ExtDB::Error> {
        let basic = if let Some(basic) = self.accounts[0].get(address) {
            basic
        } else {
            &self.forked_account_or_new(*address)?
        };

        Ok(basic.info.code_hash != KECCAK_EMPTY && basic.info.code_hash != B256::ZERO)
    }

    fn forked_account_or_new(&self, address: Address) -> Result<DbAccount, ExtDB::Error> {
        let mut basic = self
            .db
            .basic_ref(address)?
            .map(|info| DbAccount {
                info,
                ..Default::default()
            })
            .unwrap_or_else(DbAccount::new_not_existing);

        if let Some(bytecode) = basic.info.code {
            basic.info.code = Some(bytecode);
        }

        Ok(basic)
    }

    fn rollback_journal_entry(
        &mut self,
        entry: JournalEntry,
        journal_index: usize,
    ) -> JournalEntry {
        match entry {
            JournalEntry::ContractChange(code_hash, code) => {
                let old = match code {
                    Some(code) => self.contracts.insert(code_hash, code),
                    None => self.contracts.remove(&code_hash),
                };
                JournalEntry::ContractChange(code_hash, old)
            }
            JournalEntry::AccountChange(address, account) => {
                let pos = self
                    .snapshot_journal_indexes
                    .binary_search(&(journal_index + 1))
                    .unwrap_or_else(|x| x)
                    + 1;
                assert!(pos >= 1);
                let old = match account {
                    Some(account) => self.accounts[pos].insert(address, account),
                    None => self.accounts[pos].remove(&address),
                };
                JournalEntry::AccountChange(address, old)
            }
            JournalEntry::StorageChange(address, storage_change) => {
                let pos = self
                    .snapshot_journal_indexes
                    .binary_search(&(journal_index + 1))
                    .unwrap_or_else(|x| x)
                    + 1;
                assert!(pos >= 1);
                let storage = self.storage[pos].entry(address).or_default();
                let mut new_changes = HashMap::new();
                for (k, v) in storage_change {
                    if let Some(v) = v {
                        let old = storage.insert(k, v);
                        new_changes.insert(k, old);
                    } else {
                        let old = storage.remove(&k);
                        new_changes.insert(k, old);
                    }
                }
                JournalEntry::StorageChange(address, new_changes)
            }
            JournalEntry::StorageReplace(address, storage) => {
                let pos = self
                    .snapshot_journal_indexes
                    .binary_search(&(journal_index + 1))
                    .unwrap_or_else(|x| x)
                    + 1;
                assert!(pos >= 1);
                let old = match storage {
                    Some(storage) => self.storage[pos].insert(address, storage),
                    None => self.storage[pos].remove(&address),
                };
                JournalEntry::StorageReplace(address, old)
            }
        }
    }

    /*
    fn rollback_journal_entry_no_reverse(&mut self, entry: JournalEntry) {
        match entry {
            JournalEntry::ContractChange(code_hash, _) => {
                self.contracts.remove(&code_hash);
            }
            JournalEntry::AccountChange(address, account) => {
                self.accounts.last_mut().unwrap().insert(address, account);
            }
            JournalEntry::StorageChange(address, storage_change) => {
                let storage = self.storage.last_mut().unwrap().entry(address).or_default();
                for (k, v) in storage_change {
                    if let Some(v) = v {
                        storage.insert(k, v);
                    } else {
                        storage.remove(&k);
                    }
                }
            }
            JournalEntry::StorageReplace(address, storage) => {
                self.storage.last_mut().unwrap().insert(address, storage);
            }
        }
    }
    */

    /// Rolls the journal back to `point`, returning the inverse entries for
    /// [`Self::restore_rollback`].
    ///
    /// Every way of not being reachable is an error rather than a panic or a
    /// silent no-op, because rolling back "as far as possible" would replay
    /// against the wrong state and hand back a plausible, wrong result:
    ///
    /// * below the base — the entries were compacted away by history pruning;
    /// * above the tip — they were discarded by a snapshot revert;
    /// * in range but on a discarded branch — the offset was reused by the
    ///   replacement branch and no longer denotes the same state.
    ///
    /// The last one is why a bare offset is not enough. It is also not a
    /// structural invariant that can be checked once: a revert brings older
    /// history back into view, so reachability has to be decided here, at use.
    pub fn rollback(
        &mut self,
        point: JournalPoint,
    ) -> Result<Vec<JournalEntry>, JournalOutOfRange> {
        let journal_index = point.index;
        if point.epoch < self.retired_epoch_before {
            return Err(JournalOutOfRange::Retired {
                epoch: point.epoch,
                retired_before: self.retired_epoch_before,
            });
        }
        if journal_index < self.journal_base {
            return Err(JournalOutOfRange::Pruned {
                requested: journal_index,
                base: self.journal_base,
            });
        }
        let tip = self.journal_index();
        if journal_index > tip {
            return Err(JournalOutOfRange::Reverted {
                requested: journal_index,
                tip,
            });
        }
        // Only a point from an older lineage can predate a revert, so this is skipped
        // entirely for the current one — every replay in a run that has not reverted,
        // and most of them in one that has.
        if let Some((shared_index, shared_block)) = self.shared_up_to(point.epoch) {
            // At or below the cut, both the journal prefix and every block the replay
            // can reach are bit-identical, so it stays replayable. Above it, the
            // offset was reused by the replacement branch.
            //
            // The block number is part of the comparison because a revert moves blocks
            // without moving the journal: the first transaction after a run of empty
            // blocks carries exactly the snapshot's offset, and only its block places
            // it after the cut.
            if (journal_index, point.block_number) > (shared_index, shared_block) {
                return Err(JournalOutOfRange::Diverged {
                    requested: journal_index,
                    requested_block: point.block_number,
                    epoch: point.epoch,
                    current_epoch: self.journal_epoch,
                    shared_up_to: shared_index,
                    shared_block,
                });
            }
        }

        let items_to_remove = tip - journal_index;
        let mut rollback_items = Vec::with_capacity(items_to_remove);

        // `i` is absolute: `rollback_journal_entry` compares it against
        // `snapshot_journal_indexes`, which are also absolute.
        for i in (journal_index..tip).rev() {
            let entry = self.journal.pop_back().unwrap();
            let reverse_entry = self.rollback_journal_entry(entry, i);
            rollback_items.push(reverse_entry);
        }
        Ok(rollback_items)
    }

    pub fn restore_rollback(&mut self, rollback: Vec<JournalEntry>) {
        let mut journal_index = self.journal_index();

        for entry in rollback.into_iter().rev() {
            let reverse_entry = self.rollback_journal_entry(entry, journal_index);
            self.journal.push_back(reverse_entry);
            journal_index += 1;
        }
    }

    /*
    pub fn with_rollback<R>(&mut self, journal_index: usize, f: impl FnOnce() -> R) -> R {
        // roll back to the journal_index
        let items_to_remove = self.journal.len() - journal_index;
        let mut rollback_items = Vec::with_capacity(items_to_remove);
        for _ in 0..items_to_remove {
            let entry = self.journal.pop().unwrap();
            let reverse_entry = self.rollback_journal_entry(entry);
            rollback_items.push(reverse_entry);
        }

        let ret: R = f();

        // count with that `f` may commit changes to the DB, so we need to truncate the journal
        // assuming `f` does not remove any items from the journal
        let items_to_remove = self.journal.len() - journal_index;
        for _ in 0..items_to_remove {
            let entry = rollback_items.pop().unwrap();
            self.rollback_journal_entry_no_reverse(entry);
        }

        // re-apply rollback
        for entry in rollback_items.into_iter().rev() {
            self.rollback_journal_entry_no_reverse(entry);
        }

        ret
    }
    */

    pub fn snapshot(&mut self) -> usize {
        self.accounts.push(HashMap::new());
        self.storage.push(HashMap::new());
        self.snapshot_journal_indexes.push(self.journal_index());

        self.accounts.len() - 2
        // last_block_number saved and restored from chain.rs
    }

    /// Reverts to `snapshot`, consuming it, and returns the journal index it was
    /// taken at. The id is validated before anything is truncated, so an invalid
    /// id leaves the DB untouched instead of panicking part-way through.
    pub fn revert_snapshot(&mut self, snapshot: usize) -> Result<usize, String> {
        if snapshot < 1 {
            return Err(format!("snapshot id must be >= 1, got {snapshot}"));
        }
        if snapshot > self.snapshot_journal_indexes.len() {
            return Err(format!(
                "snapshot id {snapshot} out of range: only {} snapshot(s)",
                self.snapshot_journal_indexes.len()
            ));
        }

        self.accounts.truncate(snapshot + 1);
        self.storage.truncate(snapshot + 1);
        assert!(self.accounts.len() >= 2);

        let journal_index = self.snapshot_journal_indexes[snapshot - 1];
        // Every revert ends the lineage, even one that discards no journal entries:
        // it still discards *blocks*, which are re-mined with different hashes, and
        // empty blocks move block history without moving the journal. Points below
        // the cut are not penalised by this — they pass the journal test and their
        // anchors are still canonical, so `rollback` keeps accepting them.
        self.cut_lineage(journal_index, self.last_block_number);
        if journal_index < self.journal_base {
            // The snapshot predates the retained journal. State is already
            // correct — the layer truncation above did that without reading a
            // single entry — so everything still held is now unreachable.
            self.journal.clear();
            self.journal_base = journal_index;
        } else {
            self.journal.truncate(journal_index - self.journal_base);
        }
        self.snapshot_journal_indexes.truncate(snapshot - 1);
        assert!(self.accounts.len() == self.snapshot_journal_indexes.len() + 2);

        Ok(journal_index)
    }

    pub fn set_balance(&mut self, address: Address, balance: U256) -> Result<(), ExtDB::Error> {
        let mut latest_db_account = None;

        for account in self.accounts.iter().rev() {
            if let Some(db_account) = account.get(&address) {
                latest_db_account = Some(db_account.clone());
                break;
            }
        }

        if latest_db_account.is_none() {
            let basic = self.forked_account_or_new(address)?;
            self.accounts[0].insert(address, basic.clone());

            latest_db_account = Some(basic);
        };

        let db_account = match self.accounts.last_mut().unwrap().entry(address) {
            Entry::Occupied(entry) => {
                self.journal.push_back(JournalEntry::AccountChange(
                    address,
                    Some(entry.get().clone()),
                ));
                entry.into_mut()
            }
            Entry::Vacant(entry) => {
                self.journal
                    .push_back(JournalEntry::AccountChange(address, None));
                entry.insert(latest_db_account.unwrap())
            }
        };

        db_account.info.balance = balance;
        if db_account.account_state == AccountState::NotExisting {
            db_account.account_state = AccountState::Touched;
        }
        Ok(())
    }

    pub fn set_code(&mut self, address: Address, code: Vec<u8>) -> Result<(), ExtDB::Error> {
        let mut latest_db_account = None;

        for account in self.accounts.iter().rev() {
            if let Some(db_account) = account.get(&address) {
                latest_db_account = Some(db_account.clone());
                break;
            }
        }

        if latest_db_account.is_none() {
            let basic = self.forked_account_or_new(address)?;
            self.accounts[0].insert(address, basic.clone());

            latest_db_account = Some(basic);
        };

        let db_account = match self.accounts.last_mut().unwrap().entry(address) {
            Entry::Occupied(entry) => {
                self.journal.push_back(JournalEntry::AccountChange(
                    address,
                    Some(entry.get().clone()),
                ));
                entry.into_mut()
            }
            Entry::Vacant(entry) => {
                self.journal
                    .push_back(JournalEntry::AccountChange(address, None));
                entry.insert(latest_db_account.unwrap())
            }
        };

        db_account.info.code = Some(Bytecode::new_legacy(code.into()));
        db_account.info.code_hash = db_account.info.code.as_ref().unwrap().hash_slow();

        if db_account.account_state == AccountState::NotExisting {
            db_account.account_state = AccountState::Touched;
        }

        Ok(())
    }

    pub fn set_storage(
        &mut self,
        address: Address,
        index: U256,
        value: U256,
    ) -> Result<(), ExtDB::Error> {
        let storage = self.storage.last_mut().unwrap().entry(address).or_default();

        let prev_value = storage.insert(index, value);
        self.journal.push_back(JournalEntry::StorageChange(
            address,
            HashMap::from([(index, prev_value)]),
        ));

        Ok(())
    }

    pub fn set_nonce(&mut self, address: Address, nonce: u64) -> Result<(), ExtDB::Error> {
        let mut latest_db_account = None;

        for account in self.accounts.iter().rev() {
            if let Some(db_account) = account.get(&address) {
                latest_db_account = Some(db_account.clone());
                break;
            }
        }

        if latest_db_account.is_none() {
            let basic = self.forked_account_or_new(address)?;
            self.accounts[0].insert(address, basic.clone());

            latest_db_account = Some(basic);
        };

        let db_account = match self.accounts.last_mut().unwrap().entry(address) {
            Entry::Occupied(entry) => {
                self.journal.push_back(JournalEntry::AccountChange(
                    address,
                    Some(entry.get().clone()),
                ));
                entry.into_mut()
            }
            Entry::Vacant(entry) => {
                self.journal
                    .push_back(JournalEntry::AccountChange(address, None));
                entry.insert(latest_db_account.unwrap())
            }
        };

        db_account.info.nonce = nonce;

        if db_account.account_state == AccountState::NotExisting {
            db_account.account_state = AccountState::Touched;
        }
        Ok(())
    }

    /// Inserts the account's code into the cache.
    ///
    /// Accounts objects and code are stored separately in the cache, this will take the code from the account and instead map it to the code hash.
    ///
    /// Note: This will not insert into the underlying external database.
    pub fn insert_contract(&mut self, account: &mut AccountInfo) {
        if let Some(code) = &account.code {
            if !code.is_empty() {
                if account.code_hash == KECCAK_EMPTY {
                    account.code_hash = code.hash_slow();
                }
                self.contracts.entry(account.code_hash).or_insert_with(|| {
                    // previous value was unset
                    self.journal
                        .push_back(JournalEntry::ContractChange(account.code_hash, None));
                    code.clone()
                });
            }
        }
        if account.code_hash.is_zero() {
            account.code_hash = KECCAK_EMPTY;
        }
    }

    /// Insert account info but not override storage
    pub fn insert_account_info(&mut self, address: Address, mut info: AccountInfo) {
        self.insert_contract(&mut info);
        self.accounts
            .last_mut()
            .unwrap()
            .entry(address)
            .or_default()
            .info = info;
    }
}

impl<ExtDB: DatabaseRef> CacheDB<ExtDB> {
    /// Returns the account for the given address.
    ///
    /// If the account was not found in the cache, it will be loaded from the underlying database.
    pub fn load_account(&mut self, address: Address) -> Result<&mut DbAccount, ExtDB::Error> {
        let db = &self.db;
        match self.accounts.last_mut().unwrap().entry(address) {
            Entry::Occupied(entry) => Ok(entry.into_mut()),
            Entry::Vacant(entry) => Ok(entry.insert(
                db.basic_ref(address)?
                    .map(|info| DbAccount {
                        info,
                        ..Default::default()
                    })
                    .unwrap_or_else(DbAccount::new_not_existing),
            )),
        }
    }

    /*
    /// insert account storage without overriding account info
    pub fn insert_account_storage(
        &mut self,
        address: Address,
        slot: U256,
        value: U256,
    ) -> Result<(), ExtDB::Error> {
        let account = self.load_account(address)?;
        account.storage.insert(slot, value);
        Ok(())
    }

    /// replace account storage without overriding account info
    pub fn replace_account_storage(
        &mut self,
        address: Address,
        storage: HashMap<U256, U256>,
    ) -> Result<(), ExtDB::Error> {
        let account = self.load_account(address)?;
        account.account_state = AccountState::StorageCleared;
        account.storage = storage.into_iter().collect();
        Ok(())
    }
    */
}

impl<ExtDB: DatabaseRef> DatabaseCommit for CacheDB<ExtDB> {
    fn commit(&mut self, changes: AddressMap<Account>) {
        for (address, mut account) in changes {
            if !account.is_touched() {
                continue;
            }

            if account.is_selfdestructed() {
                let prev_account = match self.accounts.last_mut().unwrap().entry(address) {
                    Entry::Occupied(mut entry) => {
                        let prev_state = mem::replace(
                            &mut entry.get_mut().account_state,
                            AccountState::NotExisting,
                        );
                        let prev_info =
                            mem::replace(&mut entry.get_mut().info, AccountInfo::default());
                        let prev_locally_created = mem::replace(
                            &mut entry.get_mut().locally_created,
                            false,
                        );

                        Some(DbAccount {
                            info: prev_info,
                            account_state: prev_state,
                            locally_created: prev_locally_created,
                        })
                    }
                    Entry::Vacant(entry) => {
                        entry.insert(DbAccount::new_not_existing());

                        None
                    }
                };
                self.journal
                    .push_back(JournalEntry::AccountChange(address, prev_account));

                let prev_storage = match self.storage.last_mut().unwrap().entry(address) {
                    Entry::Occupied(mut entry) => {
                        let prev_storage = mem::take(entry.get_mut());
                        Some(prev_storage)
                    }
                    Entry::Vacant(entry) => {
                        entry.insert(HashMap::new());
                        None
                    }
                };
                self.journal
                    .push_back(JournalEntry::StorageReplace(address, prev_storage));

                continue;
            }

            self.insert_contract(&mut account.info);

            let prev_account = match self.accounts.last_mut().unwrap().entry(address) {
                Entry::Occupied(mut entry) => {
                    let prev_state =
                        mem::replace(&mut entry.get_mut().account_state, AccountState::Touched);
                    let prev_info = mem::replace(&mut entry.get_mut().info, account.info);
                    let prev_locally_created = entry.get().locally_created;

                    Some(DbAccount {
                        info: prev_info,
                        account_state: prev_state,
                        locally_created: prev_locally_created,
                    })
                }
                Entry::Vacant(entry) => {
                    let locally_created = account.is_created();
                    entry.insert(DbAccount {
                        info: account.info,
                        account_state: AccountState::Touched,
                        locally_created,
                    });

                    None
                }
            };

            let mut prev_storage = HashMap::new();
            let current_storage = self.storage.last_mut().unwrap().entry(address).or_default();

            for (key, value) in account.storage {
                prev_storage.insert(key, current_storage.insert(key, value.present_value));
            }

            self.journal
                .push_back(JournalEntry::AccountChange(address, prev_account));
            self.journal
                .push_back(JournalEntry::StorageChange(address, prev_storage));
        }
    }
}

impl<ExtDB: DatabaseRef> Database for CacheDB<ExtDB> {
    type Error = ExtDB::Error;

    fn basic(&mut self, address: Address) -> Result<Option<AccountInfo>, Self::Error> {
        for account in self.accounts.iter().rev() {
            if let Some(db_account) = account.get(&address) {
                return Ok(db_account.info());
            }
        }
        let basic = self.forked_account_or_new(address)?;
        let info = basic.info();
        self.accounts[0].insert(address, basic);
        Ok(info)
    }

    fn code_by_hash(&mut self, code_hash: B256) -> Result<Bytecode, Self::Error> {
        match self.contracts.entry(code_hash) {
            Entry::Occupied(entry) => Ok(entry.get().clone()),
            Entry::Vacant(entry) => {
                // if you return code bytes when basic fn is called this function is not needed.
                Ok(entry.insert(self.db.code_by_hash_ref(code_hash)?).clone())
            }
        }
    }

    /// Get the value in an account's storage slot.
    ///
    /// It is assumed that account is already loaded.
    fn storage(&mut self, address: Address, index: U256) -> Result<U256, Self::Error> {
        let mut locally_created = false;

        for (accounts, storage) in zip(self.accounts.iter().rev(), self.storage.iter().rev()) {
            if let Some(account) = accounts.get(&address) {
                if account.locally_created {
                    locally_created = true;
                }
                if account.account_state == AccountState::NotExisting {
                    return Ok(U256::ZERO);
                }
            }
            if let Some(storage) = storage.get(&address) {
                if let Some(entry) = storage.get(&index) {
                    return Ok(*entry);
                }
            }
        }

        if locally_created {
            return Ok(U256::ZERO);
        }

        match self.accounts.first_mut().unwrap().entry(address) {
            Entry::Occupied(_) => {
                let value = self.db.storage_ref(address, index)?;

                match self.storage.first_mut().unwrap().entry(address) {
                    Entry::Occupied(mut entry) => {
                        entry.get_mut().insert(index, value);
                    }
                    Entry::Vacant(entry) => {
                        entry.insert(HashMap::from([(index, value)]));
                    }
                }

                return Ok(value);
            }
            Entry::Vacant(entry) => {
                let info = self.db.basic_ref(address)?;
                let value = if info.is_some() {
                    self.db.storage_ref(address, index)?
                } else {
                    U256::ZERO
                };
                entry.insert(info.into());

                match self.storage.first_mut().unwrap().entry(address) {
                    Entry::Occupied(mut entry) => {
                        entry.get_mut().insert(index, value);
                    }
                    Entry::Vacant(entry) => {
                        entry.insert(HashMap::from([(index, value)]));
                    }
                }

                return Ok(value);
            }
        }
    }

    fn block_hash(&mut self, number: u64) -> Result<B256, Self::Error> {
        if number > self.last_block_number
            || number < self.last_block_number.saturating_sub(256)
        {
            return Ok(B256::ZERO);
        }
        match self.block_hashes.entry(number) {
            Entry::Occupied(entry) => Ok(*entry.get()),
            Entry::Vacant(entry) => {
                let hash = self.db.block_hash_ref(number)?;
                entry.insert(hash);
                Ok(hash)
            }
        }
    }
}

impl<ExtDB: DatabaseRef> DatabaseRef for CacheDB<ExtDB> {
    type Error = ExtDB::Error;

    fn basic_ref(&self, address: Address) -> Result<Option<AccountInfo>, Self::Error> {
        for map in self.accounts.iter().rev() {
            if let Some(account) = map.get(&address) {
                return Ok(account.info());
            }
        }
        self.db.basic_ref(address)
    }

    fn code_by_hash_ref(&self, code_hash: B256) -> Result<Bytecode, Self::Error> {
        match self.contracts.get(&code_hash) {
            Some(entry) => Ok(entry.clone()),
            None => self.db.code_by_hash_ref(code_hash),
        }
    }

    fn storage_ref(&self, address: Address, index: U256) -> Result<U256, Self::Error> {
        let mut locally_created = false;

        for (accounts, storage) in zip(self.accounts.iter().rev(), self.storage.iter().rev()) {
            if let Some(account) = accounts.get(&address) {
                if account.locally_created {
                    locally_created = true;
                }
                if account.account_state == AccountState::NotExisting {
                    return Ok(U256::ZERO);
                }
            }
            if let Some(storage) = storage.get(&address) {
                if let Some(entry) = storage.get(&index) {
                    return Ok(*entry);
                }
            }
        }
        if locally_created {
            return Ok(U256::ZERO);
        }

        self.db.storage_ref(address, index)
    }

    fn block_hash_ref(&self, number: u64) -> Result<B256, Self::Error> {
        if number > self.last_block_number
            || number < self.last_block_number.saturating_sub(256)
        {
            return Ok(B256::ZERO);
        }
        match self.block_hashes.get(&number) {
            Some(entry) => Ok(*entry),
            None => self.db.block_hash_ref(number),
        }
    }
}

#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct DbAccount {
    pub info: AccountInfo,
    /// If account is selfdestructed or newly created, storage will be cleared.
    pub account_state: AccountState,
    pub locally_created: bool,
}

impl DbAccount {
    pub fn new_not_existing() -> Self {
        Self {
            account_state: AccountState::NotExisting,
            ..Default::default()
        }
    }

    pub fn info(&self) -> Option<AccountInfo> {
        if matches!(self.account_state, AccountState::NotExisting) {
            None
        } else {
            Some(self.info.clone())
        }
    }
}

impl From<Option<AccountInfo>> for DbAccount {
    fn from(from: Option<AccountInfo>) -> Self {
        from.map(Self::from).unwrap_or_else(Self::new_not_existing)
    }
}

impl From<AccountInfo> for DbAccount {
    fn from(info: AccountInfo) -> Self {
        Self {
            info,
            account_state: AccountState::None,
            ..Default::default()
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub enum AccountState {
    /// Before Spurious Dragon hardfork there was a difference between empty and not existing.
    /// And we are flagging it here.
    NotExisting,
    /// EVM touched this account. For newer hardfork this means it can be cleared/removed from state.
    Touched,
    /// EVM didn't interacted with this account
    #[default]
    None,
}

#[cfg(test)]
mod tests {
    use super::*;
    use revm::database::EmptyDB;

    fn new_db() -> CacheDB<EmptyDB> {
        CacheDB::new(EmptyDB::default(), 0)
    }

    impl<ExtDB: DatabaseRef> CacheDB<ExtDB> {
        /// Tag a bare offset with the current lineage, for tests that do not
        /// cross a revert.
        fn point(&self, index: usize) -> JournalPoint {
            JournalPoint {
                index,
                epoch: self.journal_epoch,
                block_number: self.last_block_number,
            }
        }

    }

    fn addr(byte: u8) -> Address {
        Address::from([byte; 20])
    }

    fn touched(locally_created: bool) -> DbAccount {
        DbAccount {
            info: AccountInfo::default(),
            account_state: AccountState::Touched,
            locally_created,
        }
    }

    #[test]
    fn compaction_keeps_stored_indices_absolute() {
        let mut db = new_db();
        let address = addr(1);

        for balance in 1..=6u64 {
            db.set_balance(address, U256::from(balance)).unwrap();
        }
        let historical = 4;
        assert_eq!(db.journal_index(), 6);

        assert_eq!(db.compact_journal(historical), historical);
        assert_eq!(db.journal_base, historical);
        assert_eq!(db.journal.len(), 2);
        // The tip is unchanged: compaction drops history, not state.
        assert_eq!(db.journal_index(), 6);
        assert_eq!(db.basic(address).unwrap().unwrap().balance, U256::from(6));

        // Rolling back to a retained index still works, and the index means the
        // same thing it did before compaction.
        let rollback = db.rollback(db.point(historical)).unwrap();
        assert_eq!(db.journal_index(), historical);
        assert_eq!(db.basic(address).unwrap().unwrap().balance, U256::from(4));

        db.restore_rollback(rollback);
        assert_eq!(db.journal_index(), 6);
        assert_eq!(db.basic(address).unwrap().unwrap().balance, U256::from(6));
    }

    #[test]
    fn rollback_below_the_base_errors_instead_of_stopping_short() {
        let mut db = new_db();
        let address = addr(1);

        for balance in 1..=4u64 {
            db.set_balance(address, U256::from(balance)).unwrap();
        }
        db.compact_journal(2);

        // A short rollback would replay against the wrong state and hand back a
        // plausible, wrong trace, so this must be an error.
        assert_eq!(
            db.rollback(db.point(1)).unwrap_err(),
            JournalOutOfRange::Pruned {
                requested: 1,
                base: 2
            }
        );
        // The failed call left the journal untouched.
        assert_eq!(db.journal_index(), 4);
        assert_eq!(db.basic(address).unwrap().unwrap().balance, U256::from(4));
    }

    #[test]
    fn rollback_above_the_tip_errors_after_a_revert() {
        let mut db = new_db();
        let address = addr(1);

        db.set_balance(address, U256::from(1)).unwrap();
        let snapshot = db.snapshot();
        db.set_balance(address, U256::from(2)).unwrap();
        db.set_balance(address, U256::from(3)).unwrap();
        let stale = db.journal_index();
        let stale_point = db.journal_point();

        db.revert_snapshot(snapshot).unwrap();
        assert_eq!(db.basic(address).unwrap().unwrap().balance, U256::from(1));

        // A transaction the caller still holds in a variable, whose journal
        // entries the revert discarded.
        assert_eq!(
            db.rollback(stale_point).unwrap_err(),
            JournalOutOfRange::Reverted {
                requested: stale,
                tip: 1
            }
        );
    }

    #[test]
    fn rollback_refuses_a_point_from_a_discarded_branch() {
        let mut db = new_db();
        let address = addr(1);

        db.set_balance(address, U256::from(1)).unwrap();
        let snapshot = db.snapshot();
        db.set_balance(address, U256::from(2)).unwrap();
        // Second transaction on the branch: strictly above the cut, so its offset
        // is the one the replacement branch reuses.
        let abandoned = db.journal_point();
        db.set_balance(address, U256::from(3)).unwrap();

        db.revert_snapshot(snapshot).unwrap();
        // Grow the replacement branch past the abandoned offset. Without the
        // lineage tag this silently becomes "in range" and replays against the
        // wrong state instead of erroring.
        db.set_balance(address, U256::from(7)).unwrap();
        db.set_balance(address, U256::from(8)).unwrap();
        assert!(db.journal_index() > abandoned.index);

        assert_eq!(
            db.rollback(abandoned).unwrap_err(),
            JournalOutOfRange::Diverged {
                requested: abandoned.index,
                requested_block: abandoned.block_number,
                epoch: 0,
                current_epoch: 1,
                shared_up_to: 1,
                shared_block: 0,
            }
        );
        // The failed call left the journal untouched.
        assert_eq!(db.journal_index(), 3);
    }

    #[test]
    fn rollback_keeps_the_prefix_shared_with_the_discarded_branch() {
        let mut db = new_db();
        let address = addr(1);

        // Recorded before the snapshot, so the revert cannot have changed the
        // state it sees; this must keep working, which is what makes a bare
        // lineage comparison too coarse.
        db.set_balance(address, U256::from(1)).unwrap();
        let shared = db.journal_point();
        db.set_balance(address, U256::from(2)).unwrap();

        let snapshot = db.snapshot();
        db.set_balance(address, U256::from(3)).unwrap();
        db.revert_snapshot(snapshot).unwrap();
        db.set_balance(address, U256::from(9)).unwrap();

        let rollback = db.rollback(shared).unwrap();
        assert_eq!(db.basic(address).unwrap().unwrap().balance, U256::from(1));
        db.restore_rollback(rollback);
        assert_eq!(db.basic(address).unwrap().unwrap().balance, U256::from(9));
    }

    #[test]
    fn nested_reverts_take_the_minimum_cut_per_lineage() {
        let mut db = new_db();
        let address = addr(1);

        db.set_balance(address, U256::from(1)).unwrap();
        let deep_snapshot = db.snapshot(); // at index 1
        db.set_balance(address, U256::from(2)).unwrap();
        let shallow_snapshot = db.snapshot(); // at index 2
        db.set_balance(address, U256::from(3)).unwrap();
        let lineage0_point = db.journal_point(); // index 3, lineage 0

        // Cut at 2 ends lineage 0, then cut at 1 ends lineage 1. A point from
        // lineage 0 must satisfy min(2, 1) = 1, not just the first cut.
        db.revert_snapshot(shallow_snapshot).unwrap();
        assert_eq!(db.journal_epoch, 1);
        let lineage1_point = db.journal_point(); // index 2, lineage 1
        db.revert_snapshot(deep_snapshot).unwrap();
        assert_eq!(db.journal_epoch, 2);

        db.set_balance(address, U256::from(5)).unwrap();
        db.set_balance(address, U256::from(6)).unwrap();

        assert!(matches!(
            db.rollback(lineage0_point).unwrap_err(),
            JournalOutOfRange::Diverged { shared_up_to: 1, .. }
        ));
        assert!(matches!(
            db.rollback(lineage1_point).unwrap_err(),
            JournalOutOfRange::Diverged { shared_up_to: 1, .. }
        ));
        // Index 1 is below every cut, so it survives both reverts.
        assert!(db.rollback(JournalPoint { index: 1, epoch: 0, block_number: 0 }).is_ok());
    }

    #[test]
    fn repeated_reverts_to_the_same_snapshot_do_not_grow_the_cut_stack() {
        let mut db = new_db();
        let address = addr(1);

        db.set_balance(address, U256::from(1)).unwrap();
        for round in 0..50u64 {
            let snapshot = db.snapshot();
            db.set_balance(address, U256::from(round + 2)).unwrap();
            db.revert_snapshot(snapshot).unwrap();
            assert_eq!(db.journal_epoch, round + 1);
        }
        assert_eq!(db.journal_cuts.len(), 1, "cuts must collapse");
    }

    #[test]
    fn a_revert_discarding_no_entries_still_ends_the_lineage() {
        let mut db = new_db();
        let address = addr(1);

        db.set_balance(address, U256::from(1)).unwrap();
        let point = db.journal_point();

        // Reverting to the tip discards no journal entries, but it does discard
        // *blocks*, which are re-mined with different hashes. Keeping the lineage
        // here left the divergence check switched off for exactly the points a
        // revert invalidates - empty blocks and calls move block history without
        // writing a single journal entry.
        let snapshot = db.snapshot();
        db.revert_snapshot(snapshot).unwrap();
        assert_eq!(db.journal_epoch, 1, "the lineage must end");

        // The shared prefix pays nothing for it: the journal test passes and the
        // anchor is still canonical, so this stays replayable.
        assert!(db.rollback(point).is_ok());
    }

    #[test]
    fn rollback_refuses_a_point_recorded_on_a_later_block() {
        let mut db = CacheDB::new(EmptyDB::default(), 40);
        let address = addr(1);

        db.set_balance(address, U256::from(1)).unwrap();
        let snapshot = db.snapshot();

        // Empty blocks write nothing, so this point carries exactly the snapshot's
        // journal offset and only its block places it after the cut.
        db.last_block_number = 45;
        let after = db.journal_point();
        assert_eq!(after.index, db.journal_index(), "no journal movement");

        db.last_block_number = 40; // the revert restores the tip first
        db.revert_snapshot(snapshot).unwrap();

        let err = db.rollback(after).unwrap_err();
        assert!(
            matches!(
                err,
                JournalOutOfRange::Diverged {
                    requested_block: 45,
                    shared_block: 40,
                    ..
                }
            ),
            "expected a block-ordering rejection, got {err:?}"
        );

        // A point recorded at the same offset but at or below the snapshot's block is
        // genuinely shared history and stays replayable.
        let shared = JournalPoint {
            index: after.index,
            epoch: 0,
            block_number: 40,
        };
        assert!(db.rollback(shared).is_ok());
    }

    #[test]
    fn compaction_retires_cut_records_it_passes() {
        let mut db = new_db();
        let address = addr(1);

        // Increasing cuts: snapshot at the advancing tip, temporary write, revert,
        // permanent write. `cut_lineage` cannot collapse these, so without
        // retirement the stack grew by one per iteration.
        for round in 0..200u64 {
            let snapshot = db.snapshot();
            db.set_balance(address, U256::from(round + 10_000)).unwrap();
            db.revert_snapshot(snapshot).unwrap();
            db.set_balance(address, U256::from(round)).unwrap();
        }
        let stale = JournalPoint { index: 1, epoch: 1, block_number: 0 };
        assert!(db.journal_cuts.len() > 100, "increasing cuts do not collapse");

        db.compact_journal(db.journal_index());
        assert!(
            db.journal_cuts.len() <= db.journal.len() + 1,
            "cuts must be bounded by the retained journal, got {}",
            db.journal_cuts.len()
        );
        // A point from a retired lineage is rejected outright rather than being
        // measured against a cut record that no longer exists.
        assert!(matches!(
            db.rollback(stale).unwrap_err(),
            JournalOutOfRange::Retired { .. } | JournalOutOfRange::Pruned { .. }
        ));
    }

    #[test]
    fn reverting_to_a_snapshot_below_the_base_restores_state() {
        let mut db = new_db();
        let address = addr(1);

        db.set_balance(address, U256::from(1)).unwrap();
        let snapshot = db.snapshot();
        for balance in 2..=5u64 {
            db.set_balance(address, U256::from(balance)).unwrap();
        }

        // Compact past the snapshot's index — legal, because reverting restores
        // state by truncating the layer stack and never reads an entry.
        db.compact_journal(4);
        assert!(db.snapshot_journal_indexes[snapshot - 1] < db.journal_base);

        db.revert_snapshot(snapshot).unwrap();
        assert_eq!(db.basic(address).unwrap().unwrap().balance, U256::from(1));
        // `journal.truncate(idx - journal_base)` would have underflowed here.
        assert_eq!(db.journal_base, 1);
        assert_eq!(db.journal_index(), 1);
        assert!(db.journal.is_empty());
    }

    #[test]
    fn compaction_is_clamped_at_both_ends() {
        let mut db = new_db();
        let address = addr(1);

        db.set_balance(address, U256::from(1)).unwrap();
        db.set_balance(address, U256::from(2)).unwrap();

        // Past the tip: keeps the tip, drops everything below it.
        assert_eq!(db.compact_journal(99), 2);
        assert_eq!(db.journal_base, 2);
        assert!(db.journal.is_empty());

        // Below the base: a stale floor must not move the base backwards.
        assert_eq!(db.compact_journal(0), 2);
        assert_eq!(db.journal_base, 2);
    }

    #[test]
    fn block_hash_pruning_keeps_the_blockhash_horizon() {
        let mut db = CacheDB::new(EmptyDB::default(), 300);
        for number in 0..=300u64 {
            db.block_hashes.insert(number, B256::from([number as u8; 32]));
        }

        // The horizon is [last - 256, last] inclusive - 257 entries - and it is
        // the floor regardless of what else the chain retains.
        db.prune_block_hashes(&[]);
        assert_eq!(db.block_hashes.len(), 257);
        assert!(db.block_hashes.contains_key(&44));
        assert!(!db.block_hashes.contains_key(&43));
        assert_eq!(
            Database::block_hash(&mut db, 44).unwrap(),
            B256::from([44; 32])
        );
    }

    #[test]
    fn block_hash_pruning_drops_bands_above_the_tip() {
        let mut db = CacheDB::new(EmptyDB::default(), 0);
        for number in 0..=100u64 {
            db.block_hashes.insert(number, B256::from([number as u8; 32]));
        }

        // Three rebranch attempts from block 100 with descending peaks, spaced
        // further apart than a horizon so their bands cannot overlap. Each
        // abandoned peak used to strand its whole 257-block horizon above the
        // restored tip, so they accumulated one band per attempt.
        for peak in [5000u64, 3000, 1000] {
            for number in 101..=peak {
                db.block_hashes.insert(number, B256::from([number as u8; 32]));
            }
            db.last_block_number = peak;
            db.prune_block_hashes(&[100]); // a snapshot at 100 is live
            db.last_block_number = 100; // revert to it
            db.prune_block_hashes(&[100]);
        }

        assert_eq!(
            db.block_hashes.keys().filter(|number| **number > 100).count(),
            0,
            "no band may survive above the tip"
        );
        // The reachable set is untouched.
        for number in 0..=100u64 {
            assert!(db.block_hashes.contains_key(&number), "kept {number}");
        }
    }

    #[test]
    fn block_hash_pruning_sweeps_above_the_tip_after_the_last_snapshot_is_consumed() {
        let mut db = CacheDB::new(EmptyDB::default(), 3000);
        for number in 0..=3000u64 {
            db.block_hashes.insert(number, B256::from([number as u8; 32]));
        }

        // While the snapshot at 50 is live it keeps its own horizon, so [0, 50]
        // survives the climb to 3000.
        db.prune_block_hashes(&[50]);
        assert!(db.block_hashes.contains_key(&50));
        assert!(db.block_hashes.contains_key(&3000));

        // Reverting consumes that snapshot, so the next sweep has no anchors at all
        // and a horizon floor that saturates to zero - which an early return keyed
        // on the floor treated as "nothing to do", stranding the band above.
        db.last_block_number = 50;
        db.prune_block_hashes(&[]);
        assert_eq!(db.block_hashes.len(), 51);
        assert!(db.block_hashes.contains_key(&50));
        assert!(!db.block_hashes.contains_key(&51));
        assert!(!db.block_hashes.contains_key(&3000));
    }

    #[test]
    fn block_hash_pruning_keeps_each_anchor_horizon() {
        let mut db = CacheDB::new(EmptyDB::default(), 900);
        for number in 0..=900u64 {
            db.block_hashes.insert(number, B256::from([number as u8; 32]));
        }

        // A live snapshot at block 200 can be reverted to, which would make 200
        // the tip again, so its own horizon has to survive even though the tip has
        // moved far past it and the two horizons no longer overlap.
        db.prune_block_hashes(&[200]);
        for number in 0..=200u64 {
            assert!(db.block_hashes.contains_key(&number), "anchor kept {number}");
        }
        assert!(!db.block_hashes.contains_key(&201), "between the horizons");
        assert!(!db.block_hashes.contains_key(&643), "below current horizon");
        assert!(db.block_hashes.contains_key(&644), "current horizon");

        // Reverting to it makes those numbers reachable again, and the hash is
        // simply still there - nothing has to be put back. Deriving this from the
        // retained metadata window instead would have dropped it.
        db.last_block_number = 200;
        assert_eq!(
            Database::block_hash(&mut db, 100).unwrap(),
            B256::from([100u8; 32])
        );

        // Once the snapshot is gone, so is its horizon.
        db.last_block_number = 900;
        db.prune_block_hashes(&[]);
        assert!(!db.block_hashes.contains_key(&100));
    }

    #[test]
    fn selfdestruct_clears_storage_before_recreation() {
        let mut db = new_db();
        let address = addr(1);
        let slot = U256::from(1);

        db.set_storage(address, slot, U256::from(11)).unwrap();

        let mut destroyed = Account::default();
        destroyed.mark_touch();
        destroyed.mark_selfdestruct();
        let mut changes = AddressMap::default();
        changes.insert(address, destroyed);
        db.commit(changes);

        let mut recreated = Account::default();
        recreated.mark_touch();
        recreated.mark_created();
        let mut changes = AddressMap::default();
        changes.insert(address, recreated);
        db.commit(changes);

        assert_eq!(db.storage(address, slot).unwrap(), U256::ZERO);
    }

    #[test]
    fn storage_replace_rollback_targets_owning_snapshot_layer() {
        let mut db = new_db();
        let address = addr(1);
        let old_slot = U256::from(1);
        let newer_slot = U256::from(2);

        db.snapshot();
        db.set_storage(address, old_slot, U256::from(11)).unwrap();
        let journal_index = db.journal.len();

        let mut destroyed = Account::default();
        destroyed.mark_touch();
        destroyed.mark_selfdestruct();
        let mut changes = AddressMap::default();
        changes.insert(address, destroyed);
        db.commit(changes);

        db.snapshot();
        db.storage[3].insert(
            address,
            HashMap::from([(newer_slot, U256::from(22))]),
        );

        let rollback = db.rollback(db.point(journal_index)).unwrap();
        assert_eq!(
            db.storage[2].get(&address),
            Some(&HashMap::from([(old_slot, U256::from(11))]))
        );
        assert_eq!(
            db.storage[3].get(&address),
            Some(&HashMap::from([(newer_slot, U256::from(22))]))
        );

        db.restore_rollback(rollback);
        assert_eq!(db.storage[2].get(&address), Some(&HashMap::new()));
        assert_eq!(
            db.storage[3].get(&address),
            Some(&HashMap::from([(newer_slot, U256::from(22))]))
        );
    }

    #[test]
    fn account_setters_rollback_across_snapshot_boundaries() {
        let mut db = new_db();
        let balance_address = addr(1);
        let nonce_address = addr(2);
        let code_address = addr(3);
        let old_code = vec![0x60, 0x01, 0x00];
        let middle_code = vec![0x60, 0x02, 0x00];
        let new_code = vec![0x60, 0x03, 0x00];

        db.set_balance(balance_address, U256::from(100)).unwrap();
        db.set_nonce(nonce_address, 1).unwrap();
        db.set_code(code_address, old_code.clone()).unwrap();
        let historical = db.journal.len();

        db.snapshot();
        db.set_balance(balance_address, U256::from(200)).unwrap();
        db.set_nonce(nonce_address, 2).unwrap();
        db.set_code(code_address, middle_code).unwrap();

        db.snapshot();
        db.set_balance(balance_address, U256::from(300)).unwrap();
        db.set_nonce(nonce_address, 3).unwrap();
        db.set_code(code_address, new_code.clone()).unwrap();

        let rollback = db.rollback(db.point(historical)).unwrap();
        assert_eq!(db.basic(balance_address).unwrap().unwrap().balance, U256::from(100));
        assert_eq!(db.basic(nonce_address).unwrap().unwrap().nonce, 1);
        assert_eq!(
            db.basic(code_address).unwrap().unwrap().code_hash,
            Bytecode::new_legacy(old_code.into()).hash_slow()
        );

        db.restore_rollback(rollback);
        assert_eq!(db.basic(balance_address).unwrap().unwrap().balance, U256::from(300));
        assert_eq!(db.basic(nonce_address).unwrap().unwrap().nonce, 3);
        assert_eq!(
            db.basic(code_address).unwrap().unwrap().code_hash,
            Bytecode::new_legacy(new_code.into()).hash_slow()
        );
    }

    #[test]
    fn block_hash_bounds_do_not_underflow_below_block_256() {
        let mut db = CacheDB::new(EmptyDB::default(), 1);
        let hash = B256::from([1; 32]);
        db.block_hashes.insert(0, hash);

        assert_eq!(Database::block_hash(&mut db, 0).unwrap(), hash);
        assert_eq!(DatabaseRef::block_hash_ref(&db, 0).unwrap(), hash);
        assert_eq!(Database::block_hash(&mut db, 2).unwrap(), B256::ZERO);
        assert_eq!(DatabaseRef::block_hash_ref(&db, 2).unwrap(), B256::ZERO);
    }

    #[test]
    fn restore_rollback_restores_contract_code_mapping() {
        let mut db = new_db();
        let journal_index = db.journal.len();
        let code = Bytecode::new_legacy(vec![0x60, 0x01, 0x00].into());
        let mut info = AccountInfo {
            code: Some(code.clone()),
            ..Default::default()
        };

        db.insert_contract(&mut info);
        let code_hash = info.code_hash;
        assert_eq!(db.contracts.get(&code_hash), Some(&code));

        let rollback = db.rollback(db.point(journal_index)).unwrap();
        assert!(!db.contracts.contains_key(&code_hash));

        db.restore_rollback(rollback);
        assert_eq!(db.contracts.get(&code_hash), Some(&code));
        assert_eq!(Database::code_by_hash(&mut db, code_hash).unwrap(), code);
    }

    #[test]
    fn identifies_forked_contract_from_code_hash_without_loaded_code() {
        let mut db = new_db();
        let address = addr(1);
        let code_hash = B256::from([1; 32]);
        db.accounts[0].insert(
            address,
            DbAccount::from(AccountInfo {
                code_hash,
                code: None,
                ..Default::default()
            }),
        );

        assert!(db.is_contract_forked(&address).unwrap());
        assert!(!db.is_contract_forked(&addr(2)).unwrap());
    }
}
