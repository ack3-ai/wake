use alloy::eips::{BlockId, BlockNumberOrTag};
use alloy::providers::{Provider, ProviderBuilder, RootProvider, WsConnect};
use alloy::rpc::client::ClientBuilder;
use alloy::rpc::types::Block as AlloyBlock;
use alloy::transports::{RpcError, TransportErrorKind};
use auto_impl::auto_impl;
use num_bigint::BigUint;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::types::{PyBytes, PyDict, PyNone, PyString, PyTuple, PyType};
use send_wrapper::SendWrapper;
use rand_xoshiro::rand_core::SeedableRng;
use rand_xoshiro::Xoshiro256PlusPlus;
use rand::Rng;
use revm::context::result::{EVMError, ExecutionResult};
use revm::context::transaction::AccessList;
use revm::context::{BlockEnv, Cfg, CfgEnv, ContextTr, Evm, TxEnv};
use revm::database::{AlloyDB, EmptyDB, WrapDatabaseAsync};
use revm::handler::instructions::EthInstructions;
use revm::inspector::{InspectCommitEvm, InspectEvm};
use revm::handler::{EthFrame, EthPrecompiles};
use revm::inspector::JournalExt;
use revm::interpreter::interpreter::EthInterpreter;
use revm::precompile::{PrecompileSpecId, Precompiles};
use revm::primitives::hardfork::SpecId;
use revm::primitives::{Address as RevmAddress, Bytes, Log, B256, U256};
use std::collections::HashMap;
use std::mem;
use std::ops::AddAssign;
use std::time::{SystemTime, UNIX_EPOCH};

use pyo3::{intern, prelude::*, IntoPyObjectExt, PyTypeInfo};
use std::sync::Arc;

use crate::call::Call;
use crate::chain_snapshot::ChainSnapshot;
use crate::inspectors::access_list_inspector::AccessListInspector;
use crate::account::Account;
use crate::address::Address;
use crate::blocks::{Block, Blocks};
use crate::chain_interface::ChainInterface;
use crate::inspectors::console_log_inspector::ConsoleLogInspector;
use crate::contract::Contract;
use crate::inspectors::coverage_inspector::CoverageInspector;
use crate::db::{DB, DBError};
use crate::enums::{
    AccessListEnum, AddressEnum, BlockEnum, GasLimitEnum, RequestTypeEnum, ValueEnum,
};
use crate::evm::prepare_tx_env;
use crate::inspectors::fqn_inspector::{ErrorMetadata, EventMetadata, FqnInspector};
use crate::globals::{DEFAULT_CHAIN, TOKIO_RUNTIME};
use crate::pytypes::decode_and_normalize;
use crate::tx::TransactionAbc;
use crate::txs::Txs;
use crate::utils::get_py_objects;
use crate::memory_db::{CacheDB, JournalPoint};
use revm::{Context, Inspector};
use url::Url;

/// Balance assigned to every development account when a chain connects, in wei.
///
/// Anvil pre-funds its development accounts with 10000 ETH; matching that value keeps
/// a test suite behaving the same whichever engine `testing.cmd` selects.
const DEFAULT_ACCOUNT_BALANCE_WEI: u128 = 10_000 * 1_000_000_000_000_000_000;

/// Accepted hardfork spellings, in fork order. Used for the error message only.
const HARDFORK_NAMES: [&str; 15] = [
    "Frontier",
    "Homestead",
    "Tangerine",
    "Spurious",
    "Byzantium",
    "Petersburg",
    "Istanbul",
    "Berlin",
    "London",
    "Merge",
    "Shanghai",
    "Cancun",
    "Prague",
    "Osaka",
    "Amsterdam",
];

/// Resolve a hardfork name to a revm [`SpecId`].
///
/// revm's own `FromStr` accepts exactly one spelling per fork, so `"cancun"` and
/// `"SpuriousDragon"` are both rejected by it. Accept any capitalization, ignore `-`
/// and `_` separators, and allow the widely used name for the three forks revm
/// spells differently (`Paris`, `SpuriousDragon`, `TangerineWhistle`).
///
/// Matching on the enum variants rather than on `HARDFORK_NAMES` keeps this
/// compile-checked: a fork renamed or removed upstream breaks the build here
/// instead of silently failing to parse at runtime.
fn parse_hardfork(hardfork: &str) -> PyResult<SpecId> {
    let key: String = hardfork
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .map(|c| c.to_ascii_lowercase())
        .collect();

    Ok(match key.as_str() {
        "frontier" => SpecId::FRONTIER,
        "homestead" => SpecId::HOMESTEAD,
        "tangerine" | "tangerinewhistle" => SpecId::TANGERINE,
        "spurious" | "spuriousdragon" => SpecId::SPURIOUS_DRAGON,
        "byzantium" => SpecId::BYZANTIUM,
        "petersburg" => SpecId::PETERSBURG,
        "istanbul" => SpecId::ISTANBUL,
        "berlin" => SpecId::BERLIN,
        "london" => SpecId::LONDON,
        "merge" | "paris" => SpecId::MERGE,
        "shanghai" => SpecId::SHANGHAI,
        "cancun" => SpecId::CANCUN,
        "prague" => SpecId::PRAGUE,
        "osaka" => SpecId::OSAKA,
        "amsterdam" => SpecId::AMSTERDAM,
        _ => {
            return Err(PyValueError::new_err(format!(
                "Invalid hardfork `{}`; expected one of: {}",
                hardfork,
                HARDFORK_NAMES.join(", ")
            )));
        }
    })
}

use crate::inspectors::trace_inspector::{NativeTrace, TraceInspector};

use tokio;

#[pyfunction]
pub fn default_chain(py: Python) -> PyResult<Py<Chain>> {
    let mut chain = DEFAULT_CHAIN.lock().unwrap();
    match chain.as_ref() {
        Some(chain) => Ok(chain.clone_ref(py)),
        None => {
            *chain = Some(Chain::new(py)?);
            Ok(chain.as_ref().unwrap().clone_ref(py))
        }
    }
}

#[derive(Clone)]
pub(crate) struct ProviderWrapper(RootProvider);

impl ProviderWrapper {
    pub(crate) fn get_block_by_number(
        &self,
        py: Python,
        number: u64,
        with_transactions: bool,
        handle: &tokio::runtime::Handle,
    ) -> Result<Option<AlloyBlock>, RpcError<TransportErrorKind>> {
        py.detach(|| {
            let future = self.0.get_block_by_number(BlockNumberOrTag::Number(number));
            if with_transactions {
                handle.block_on(async { future.full().await })
            } else {
                handle.block_on(async { future.await })
            }
        })
    }
}

#[auto_impl(&mut)]
pub(crate) trait InspectorExt<CTX: ContextTr<Journal: JournalExt>>: Inspector<CTX> {}

impl<CTX: ContextTr<Journal: JournalExt>> InspectorExt<CTX> for AccessListInspector {}
impl<CTX: ContextTr<Journal: JournalExt>> InspectorExt<CTX> for FqnInspector {}
impl<CTX: ContextTr<Journal: JournalExt>> InspectorExt<CTX> for TraceInspector {}
impl<CTX: ContextTr<Journal: JournalExt>> InspectorExt<CTX> for CoverageInspector {}
impl<CTX: ContextTr<Journal: JournalExt>> InspectorExt<CTX> for ConsoleLogInspector {}

trait FqnInspectorExt<CTX: ContextTr<Journal: JournalExt>>: InspectorExt<CTX> + Send {
    fn into_metadata(
        self: Box<Self>,
    ) -> (HashMap<[u8; 4], ErrorMetadata>, HashMap<Log, EventMetadata>);
    fn errors_metadata(&self) -> &HashMap<[u8; 4], ErrorMetadata>;
    fn sync_coverage(&mut self, py: Python) -> PyResult<()>;
}

impl<CTX: ContextTr<Journal: JournalExt>> FqnInspectorExt<CTX> for FqnInspector {
    fn into_metadata(
        self: Box<Self>,
    ) -> (HashMap<[u8; 4], ErrorMetadata>, HashMap<Log, EventMetadata>) {
        (self.errors_metadata, self.events_metadata)
    }
    fn errors_metadata(&self) -> &HashMap<[u8; 4], ErrorMetadata> {
        &self.errors_metadata
    }
    fn sync_coverage(&mut self, _: Python) -> PyResult<()> {
        // nothing to do
        Ok(())
    }
}

impl<CTX: ContextTr<Journal: JournalExt>> FqnInspectorExt<CTX> for CoverageInspector {
    fn into_metadata(
        self: Box<Self>,
    ) -> (HashMap<[u8; 4], ErrorMetadata>, HashMap<Log, EventMetadata>) {
        (
            self.fqn_inspector.errors_metadata,
            self.fqn_inspector.events_metadata,
        )
    }
    fn errors_metadata(&self) -> &HashMap<[u8; 4], ErrorMetadata> {
        &self.fqn_inspector.errors_metadata
    }
    fn sync_coverage(&mut self, py: Python) -> PyResult<()> {
        self.update_coverage(py)
    }
}

pub(crate) type CustomContext = Context<BlockEnv, TxEnv, CfgEnv, DB>;

pub(crate) type CustomEvm = Evm<
    CustomContext,
    (),
    EthInstructions<EthInterpreter, CustomContext>,
    EthPrecompiles,
    EthFrame<EthInterpreter>,
>;

/// Wrapper that makes a non-Send/Sync type usable in `#[pyclass]` (which requires Send + Sync).
///
/// SAFETY: Chain is only ever accessed from one thread at a time
/// (Python GIL ensures exclusive access via borrow/borrow_mut), and `py.allow_threads()`
/// executes closures on the calling OS thread (only the GIL is released).
/// The non-Send/Sync root cause is `Rc<RefCell<Vec<u8>>>` inside revm's `LocalContext`.
pub(crate) struct EvmCell(CustomEvm);
unsafe impl Send for EvmCell {}
unsafe impl Sync for EvmCell {}

impl EvmCell {
    pub fn new(evm: CustomEvm) -> Self {
        Self(evm)
    }

    /// Consume the wrapper and return the inner EVM.
    pub fn into_inner(self) -> CustomEvm {
        self.0
    }
}

impl std::ops::Deref for EvmCell {
    type Target = CustomEvm;
    fn deref(&self) -> &CustomEvm {
        &self.0
    }
}

impl std::ops::DerefMut for EvmCell {
    fn deref_mut(&mut self) -> &mut CustomEvm {
        &mut self.0
    }
}

pub enum BlockInfo {
    Mined(Py<Block>),
    Pending(BlockEnv),
}

#[pyclass]
pub struct Chain {
    rng: Xoshiro256PlusPlus,
    pub(crate) evm: Option<EvmCell>,
    pub(crate) provider: Option<ProviderWrapper>,
    pub labels: Arc<HashMap<RevmAddress, String>>,
    collect_coverage: bool,

    pub deployed_libraries: Arc<HashMap<[u8; 17], RevmAddress>>,

    pub(crate) blocks: Option<Py<Blocks>>,
    pub(crate) txs: Option<Py<Txs>>,
    pub(crate) chain_interface: Option<Py<ChainInterface>>,
    #[pyo3(get)]
    connected: bool,
    pub(crate) chain_id: u64,
    pub(crate) forked_chain_id: Option<u64>,
    forked_block: Option<u64>,
    accounts: Vec<Py<Account>>,
    #[pyo3(get)]
    pub(crate) default_tx_account: Option<Py<Account>>,
    #[pyo3(get)]
    pub(crate) default_call_account: Option<Py<Account>>,
    #[pyo3(get)]
    pub(crate) default_estimate_account: Option<Py<Account>>,
    #[pyo3(get)]
    pub(crate) default_access_list_account: Option<Py<Account>>,
    #[pyo3(get, set)]
    pub(crate) automine: bool,
    #[pyo3(get)]
    pub(crate) block_gas_limit: u64,

    pub(crate) latest_block_env: Option<BlockEnv>,
    snapshots: Vec<ChainSnapshot>,
    pub(crate) pending_txs: Vec<Py<TransactionAbc>>,
    pub(crate) pending_gas_used: u64,

    // address => pytype
    // overrides how to resolve fqn (and pytypes) for this address
    pub(crate) fqn_overrides: Arc<HashMap<RevmAddress, Py<PyType>>>,

    #[pyo3(get, set)]
    tx_callback: Option<Py<PyAny>>,

    /// How many blocks to keep, or `None` to disable pruning and retain
    /// everything as before 5.0.
    ///
    /// Blocks are the unit of retention: a transaction lives exactly as long as
    /// its block, and the journal keeps only what the retained blocks need.
    block_history: Option<usize>,
}

// The first argument is stringified into the type's module name, so it must be
// module *tokens*: a string literal is stringified again and the quotes end up in
// `__module__`, which makes the class unimportable and therefore unpicklable.
// `wake_rs` is where `lib.rs` registers it, so the name is also true.
pyo3::create_exception!(
    wake_rs,
    HistoryPrunedError,
    pyo3::exceptions::PyException,
    "Raised when transaction or block history that has been pruned is accessed."
);

#[pymethods]
impl Chain {
    #[new]
    fn new(py: Python) -> PyResult<Py<Self>> {
        let random = Python::import(py, intern!(py, "wake.development.globals"))?
            .getattr(intern!(py, "random"))?;

        let chain = Py::new(
            py,
            Self {
                rng: Xoshiro256PlusPlus::seed_from_u64(
                    random
                        .call_method1(intern!(py, "getrandbits"), (64,))?
                        .extract()?,
                ),
                evm: None,
                provider: None,
                deployed_libraries: Arc::new(HashMap::new()),
                blocks: None,
                txs: None,
                chain_interface: None,
                labels: Arc::new(HashMap::new()),
                collect_coverage: false,
                connected: false,
                chain_id: 0,
                forked_chain_id: None,
                forked_block: None,
                accounts: vec![],
                default_tx_account: None,
                default_call_account: None,
                default_estimate_account: None,
                default_access_list_account: None,
                automine: true,
                block_gas_limit: 30000000_u64,
                latest_block_env: None,
                snapshots: vec![],
                pending_txs: vec![],
                pending_gas_used: 0,
                fqn_overrides: Arc::new(HashMap::new()),
                tx_callback: None,
                block_history: None,
            },
        )?;
        chain.borrow_mut(py).chain_interface =
            Some(Py::new(py, ChainInterface::new(chain.clone_ref(py)))?);
        Ok(chain)
    }

    pub fn __hash__(&self) -> u64 {
        self as *const _ as usize as u64
    }

    fn __deepcopy__<'py>(slf: &Bound<'py, Self>, _memo: Option<Py<PyAny>>) -> Bound<'py, Self> {
        slf.clone()
    }

    #[setter]
    fn set_default_call_account(
        slf: &Bound<Self>,
        py: Python,
        account: Option<AddressEnum>,
    ) -> PyResult<()> {
        slf.borrow_mut().default_call_account = match account {
            Some(account) => Some(Py::new(
                py,
                Account::from_revm_address(
                    py,
                    account.try_into()?,
                    slf.clone().unbind().into_any(),
                )?,
            )?),
            None => None,
        };
        Ok(())
    }

    #[setter]
    fn set_default_tx_account(
        slf: &Bound<Self>,
        py: Python,
        account: Option<AddressEnum>,
    ) -> PyResult<()> {
        slf.borrow_mut().default_tx_account = match account {
            Some(account) => Some(Py::new(
                py,
                Account::from_revm_address(
                    py,
                    account.try_into()?,
                    slf.clone().unbind().into_any(),
                )?,
            )?),
            None => None,
        };
        Ok(())
    }

    #[setter]
    fn set_default_estimate_account(
        slf: &Bound<Self>,
        py: Python,
        account: Option<AddressEnum>,
    ) -> PyResult<()> {
        slf.borrow_mut().default_estimate_account = match account {
            Some(account) => Some(Py::new(
                py,
                Account::from_revm_address(
                    py,
                    account.try_into()?,
                    slf.clone().unbind().into_any(),
                )?,
            )?),
            None => None,
        };
        Ok(())
    }

    #[setter]
    fn set_default_access_list_account(
        slf: &Bound<Self>,
        py: Python,
        account: Option<AddressEnum>,
    ) -> PyResult<()> {
        slf.borrow_mut().default_access_list_account = match account {
            Some(account) => Some(Py::new(
                py,
                Account::from_revm_address(
                    py,
                    account.try_into()?,
                    slf.clone().unbind().into_any(),
                )?,
            )?),
            None => None,
        };
        Ok(())
    }

    #[getter]
    fn get_accounts<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        PyTuple::new(py, &self.accounts)
    }

    #[getter]
    fn get_chain_id(&self, py: Python) -> PyResult<Py<PyAny>> {
        get_py_objects(py).wake_u256.call1(py, (self.get_evm()?.cfg.chain_id,))
    }

    #[getter]
    fn get_forked_chain_id(&self, py: Python) -> PyResult<Py<PyAny>> {
        match self.forked_chain_id {
            Some(chain_id) => get_py_objects(py)
                .wake_u256
                .call1(py, (chain_id,)),
            None => PyNone::get(py).into_py_any(py),
        }
    }

    #[getter]
    fn get_blocks(&self, py: Python) -> Py<Blocks> {
        self.blocks.as_ref().unwrap().clone_ref(py)
    }

    #[getter]
    fn get_txs(&self, py: Python) -> Py<Txs> {
        self.txs.as_ref().unwrap().clone_ref(py)
    }

    #[getter]
    fn get_chain_interface(&self, py: Python) -> Py<ChainInterface> {
        self.chain_interface.as_ref().unwrap().clone_ref(py)
    }

    #[getter]
    fn get_coinbase(slf: Py<Self>, py: Python) -> PyResult<Account> {
        let addr = slf.borrow(py).get_evm()?.block.beneficiary;
        Account::from_revm_address(py, addr, slf.into_any())
    }

    #[setter]
    fn set_coinbase(slf: Py<Self>, py: Python, value: AddressEnum) -> PyResult<()> {
        if let AddressEnum::Account(account) = &value {
            if !account.borrow(py).chain.inner().is(&slf) {
                return Err(PyValueError::new_err(
                    "Account does not belong to this chain",
                ));
            }
        }

        slf.borrow_mut(py)
            .get_evm_mut()?
            .block
            .beneficiary = value.try_into()?;

        Ok(())
    }

    #[pyo3(signature = (account))]
    fn set_default_accounts(
        slf: &Bound<Self>,
        py: Python,
        account: Option<AddressEnum>,
    ) -> PyResult<()> {
        let acc = match account {
            Some(account) => Some(Py::new(
                py,
                Account::from_revm_address(
                    py,
                    account.try_into()?,
                    slf.clone().unbind().into_any(),
                )?,
            )?),
            None => None,
        };

        let mut borrowed = slf.borrow_mut();

        borrowed.default_tx_account = acc.as_ref().map(|a| a.clone_ref(py));
        borrowed.default_call_account = acc.as_ref().map(|a| a.clone_ref(py));
        borrowed.default_estimate_account = acc.as_ref().map(|a| a.clone_ref(py));
        borrowed.default_access_list_account = acc;

        Ok(())
    }

    #[getter(_labels)]
    fn get_labels<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let labels = PyDict::new(py);
        for (addr, label) in self.labels.iter() {
            labels.set_item(Py::new(py, Address(*addr))?, label)?;
        }
        Ok(labels)
    }

    #[setter(_labels)]
    fn set_labels(&mut self, labels: Bound<PyDict>) -> PyResult<()> {
        let new_labels = Arc::make_mut(&mut self.labels);
        new_labels.clear();

        for (addr, label) in labels.iter() {
            new_labels.insert(
                addr.cast_into::<Address>()?.borrow().0,
                label.cast_into::<PyString>()?.to_string(),
            );
        }
        Ok(())
    }

    #[setter]
    fn set_block_gas_limit(&mut self, gas_limit: u64) -> PyResult<()> {
        if gas_limit < self.pending_gas_used {
            return Err(PyValueError::new_err("Gas limit is lower than gas already used in pending block"));
        }
        self.block_gas_limit = gas_limit;
        self.get_evm_mut()?.block.gas_limit = gas_limit - self.pending_gas_used;
        Ok(())
    }

    fn dump_rng<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let b = bincode::serialize(&self.rng).map_err(|_| PyRuntimeError::new_err("Failed to serialize RNG"))?;
        Ok(PyBytes::new(py, &b).into())
    }

    fn load_rng(&mut self, rng: Vec<u8>) -> PyResult<()> {
        let rng: Xoshiro256PlusPlus = bincode::deserialize(&rng).map_err(|_| PyRuntimeError::new_err("Failed to deserialize RNG"))?;
        self.rng = rng;
        Ok(())
    }

    fn snapshot(&mut self, py: Python) -> PyResult<String> {
        let evm = self.get_evm_mut()?;
        let snapshot_id = evm.db_mut().snapshot().to_string();

        self.snapshots.push(ChainSnapshot::from_chain(self, py)?);

        Ok(snapshot_id)
    }

    fn revert(slf: &Bound<Self>, py: Python, id: &str) -> PyResult<()> {
        // A bad snapshot id must leave the chain untouched, and must surface as a
        // catchable Python exception — a Rust panic crosses the pyo3 boundary as
        // `PanicException`, which subclasses `BaseException` and so slips past
        // `except Exception`. Everything is therefore validated before the first
        // mutation, and the fallible DB unwind runs before `snapshots` is touched.
        let snapshot_id: usize = id.parse().map_err(|_| {
            PyValueError::new_err(format!("invalid snapshot id: {id:?}"))
        })?;
        if snapshot_id < 1 {
            return Err(PyValueError::new_err(format!(
                "snapshot id must be >= 1, got {snapshot_id}"
            )));
        }
        // Read the block number before unwinding anything: on a genesis snapshot
        // the `- 1` below wraps silently to u64::MAX in a release build.
        let last_block_number: u64 = {
            let borrowed = slf.borrow();
            // `Vec::truncate` past the end is a no-op, so without this check an
            // out-of-range id would silently revert to the newest snapshot instead.
            if borrowed.snapshots.len() < snapshot_id {
                return Err(PyRuntimeError::new_err(format!(
                    "stale or out-of-range snapshot id {snapshot_id}: only {} live snapshot(s)",
                    borrowed.snapshots.len()
                )));
            }
            let snapshot = &borrowed.snapshots[snapshot_id - 1];
            let number: u64 = TryInto::try_into(snapshot.pending_block_env.number)
                .map_err(|_| {
                    PyRuntimeError::new_err("snapshot block number exceeds u64 range")
                })?;
            if number < 1 {
                return Err(PyRuntimeError::new_err(
                    "cannot revert: snapshot block number would underflow (genesis)",
                ));
            }
            number - 1
        };

        // Dropped after `borrowed` is released — see `Chain::maybe_prune`.
        let garbage_txs;
        let garbage_blocks;

        {
            let mut borrowed = slf.borrow_mut();
            let evm = borrowed.get_evm_mut()?;
            // The tip is restored first so that `revert` records the block the cut
            // lands on: a point's lineage is placed by journal offset *and* block, and
            // the block is what distinguishes a point recorded after a run of empty
            // blocks from genuinely shared history at the same offset.
            evm.db_mut().set_last_block_number(last_block_number);
            let journal_index = evm
                .db_mut()
                .revert(snapshot_id)
                .map_err(PyRuntimeError::new_err)?;

            borrowed.snapshots.truncate(snapshot_id);
            // len was >= snapshot_id >= 1, so exactly `snapshot_id` elements remain.
            let snapshot = borrowed.snapshots.pop().unwrap();

            let mut blocks = borrowed.blocks.as_ref().unwrap().borrow_mut(py);
            let mut dropped_blocks = blocks.remove_blocks(last_block_number);
            let mut txs = borrowed.txs.as_ref().unwrap().borrow_mut(py);
            let mut dropped_txs = txs.remove_txs(journal_index);

            if snapshot.has_history() {
                // Pruning truncates the window from the front and the revert just
                // truncated it from the back, which together can leave nothing to
                // land on. Empty it and re-seed: the snapshot's window ends
                // exactly at the block being reverted to and reaches at least as
                // far back as whatever survived here, so a revert restores the
                // same visible depth every time — which the shrinker depends on,
                // since it reverts and replays.
                dropped_blocks.append(&mut blocks.drain_front(0));
                dropped_txs.append(&mut txs.drain_all());
            }
            drop(blocks);
            drop(txs);
            garbage_blocks = dropped_blocks;
            garbage_txs = dropped_txs;

            snapshot.restore_history(py, &borrowed)?;

            // No block hashes to put back: the revert lowers `last_block_number`
            // onto numbers this snapshot has been protecting from pruning for as
            // long as it was live.
            snapshot.restore_to_chain(&mut borrowed)?;
        }

        drop(garbage_txs);
        drop(garbage_blocks);

        Ok(())
    }

    #[pyo3(signature = (*, accounts=10, chain_id=None, fork=None, hardfork=None))]
    fn connect(
        slf: Py<Self>,
        py: Python,
        accounts: u16,
        chain_id: Option<u64>,
        fork: Option<String>,
        hardfork: Option<String>,
    ) -> PyResult<Py<PyAny>> {
        let connect_context = PyModule::import(py, "wake.utils.connect_context")?
            .getattr("ConnectContext")?
            .call1((slf.clone_ref(py), accounts, chain_id, fork, hardfork))?;
        Ok(connect_context.into())
    }

    fn snapshot_and_revert(slf: Py<Self>, py: Python) -> PyResult<Py<PyAny>> {
        let context = PyModule::import(py, "wake.utils.snapshot_revert_context")?
            .getattr("SnapshotRevertContext")?
            .call1((slf.clone_ref(py),))?;
        Ok(context.into())
    }

    fn change_automine(slf: Py<Self>, py: Python, automine: bool) -> PyResult<Py<PyAny>> {
        let automine_context = PyModule::import(py, "wake.utils.automine_context")?
            .getattr("AutomineContext")?
            .call1((slf.clone_ref(py), automine))?;
        Ok(automine_context.into())
    }

    // Parameter name matches `wake.development.core.Chain.mine`, so the keyword form
    // works identically on either engine.
    #[pyo3(name = "mine", signature = (timestamp_change=None))]
    fn mine_py(
        slf: Bound<Self>,
        py: Python,
        timestamp_change: Option<Bound<PyAny>>,
    ) -> PyResult<()> {
        if let Some(timestamp_change) = timestamp_change {
            let latest_timestamp = slf.borrow().latest_block_env.as_ref().unwrap().timestamp;
            let new_timestamp = timestamp_change
                .call1::<(u64,)>((latest_timestamp.try_into().unwrap(),))?
                .extract::<u64>()?;
            let mut borrowed = slf.borrow_mut();
            let evm = borrowed.get_evm_mut()?;
            evm.block.timestamp = new_timestamp.try_into().unwrap();
            let _ = borrowed.mine(py, true);
            drop(borrowed);
        } else {
            let _ = slf.borrow_mut().mine(py, true);
        }

        // Bare `chain.mine()` loops produce blocks with no transactions, so the
        // send path alone would never trim them.
        Chain::maybe_prune(&slf, py)?;

        Ok(())
    }

    fn set_next_block_timestamp(&mut self, new_timestamp: u64) -> PyResult<()> {
        let evm = self.get_evm_mut()?;
        evm.block.timestamp = new_timestamp.try_into().unwrap();
        Ok(())
    }

    #[pyo3(signature = (accounts, chain_id, fork_url, hardfork))]
    fn _connect(
        slf: Py<Chain>,
        py: Python,
        accounts: u16,
        chain_id: Option<u64>,
        fork_url: Option<&str>,
        hardfork: Option<&str>,
    ) -> PyResult<()> {
        let mut slf_ = slf.borrow_mut(py);

        if slf_.connected {
            return Err(PyRuntimeError::new_err("Already connected"));
        }

        // Reset all state completely
        slf_.fqn_overrides = Arc::new(HashMap::new());
        slf_.provider = None;
        slf_.blocks = None;
        slf_.txs = None;
        slf_.evm = None;
        slf_.forked_chain_id = None;
        slf_.forked_block = None;
        slf_.latest_block_env = None;
        slf_.snapshots.clear();
        slf_.pending_txs.clear();
        slf_.pending_gas_used = 0;
        slf_.accounts.clear();
        slf_.deployed_libraries = Arc::new(HashMap::new());
        slf_.labels = Arc::new(HashMap::new());
        slf_.default_tx_account = None;
        slf_.default_call_account = None;
        slf_.default_estimate_account = None;
        slf_.default_access_list_account = None;

        // set automine to true for always, so change requre in test function.
        // Some of value can be set outside of connection.
        // This might confuse if tester set it at global level with global variable.
        // however, if multiple test function is run without reset, it will remain the config from previous test.
        slf_.automine = true;
        slf_.tx_callback = None;

        slf_.connected = true;

        let py_objects = get_py_objects(py);
        py_objects
            .wake_connected_chains
            .bind(py)
            .append(slf.clone_ref(py))?;

        slf_.collect_coverage = py
            .import("wake.testing.native_coverage")?
            .getattr("collect_coverage")?
            .extract::<bool>()?;

        // Re-read retention from config on every connect, for the same reason
        // `automine` is reset above: a value set on the chain outside a
        // connection must not leak into the next test.
        let testing_config = py
            .import("wake.development.globals")?
            .call_method0("get_config")?
            .getattr("testing")?;
        slf_.block_history = testing_config
            .getattr("block_history")?
            .extract::<Option<usize>>()?
            .map(|keep| keep.max(1));

        for i in 0..accounts {
            slf_.accounts.push(Py::new(
                py,
                Account::from_mnemonic(
                    &Account::type_object(py),
                    py,
                    "test test test test test test test test test test test junk",
                    "",
                    format!("m/44'/60'/0'/0/{}", i).as_str(),
                    Some(slf.clone_ref(py).into_py_any(py)?), // TODO optimize
                )?,
            )?);
        }
        slf_.default_call_account = Some(slf_.accounts[0].clone_ref(py));
        slf_.default_tx_account = Some(slf_.accounts[0].clone_ref(py));
        slf_.default_estimate_account = Some(slf_.accounts[0].clone_ref(py));
        slf_.default_access_list_account = Some(slf_.accounts[0].clone_ref(py));

        //let runtime = tokio::runtime::Runtime::new().unwrap();
        let runtime = &TOKIO_RUNTIME;
        /*
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        */

        // TODO handler config

        let spec = match hardfork {
            Some(hardfork) => parse_hardfork(hardfork)?,
            None => SpecId::default(),
        };


        match fork_url {
            Some(url) => {
                let mut parts = url.split('@');
                let url = parts.next().unwrap_or_default().to_string();
                let block = parts
                    .next()
                    .and_then(|b| b.parse::<u64>().ok())
                    .map(BlockId::from)
                    .unwrap_or(BlockId::latest());

                let (provider, forked_block, forked_chain_id) = py.detach(|| {
                    let provider = if url.starts_with("ws://") || url.starts_with("wss://") {
                        Arc::new(ProviderBuilder::new().connect_client(
                            runtime
                                .block_on(ClientBuilder::default().ws(WsConnect::new(url)))
                                .unwrap(),
                        ))
                    } else {
                        Arc::new(ProviderBuilder::new().connect_client(
                            ClientBuilder::default().http(Url::parse(&url).unwrap()),
                        ))
                    };

                    let forked_block = runtime
                        .block_on(async { provider.get_block(block).await })
                        .unwrap()
                        .unwrap();
                    let forked_chain_id = runtime
                        .block_on(async { provider.get_chain_id().await })
                        .unwrap();

                    (provider, forked_block, forked_chain_id)
                });

                slf_.provider = Some(ProviderWrapper(provider.root().clone()));

                let mut db = CacheDB::new(
                    WrapDatabaseAsync::with_handle(
                        AlloyDB::new(provider, forked_block.header.number.into()),
                        runtime.handle().clone(),
                    ),
                    forked_block.header.number,
                );

                let path = format!(
                    ".wake/fork_cache/{}/{}/state.db",
                    forked_chain_id, forked_block.header.number
                );
                if std::path::Path::new(&path).is_file() {
                    if let Err(e) = db.load_forked_state(&path) {
                        log::warn!("Failed to load cached forking state: {}", e);
                    }
                }

                let mut evm: CustomEvm = Evm::new(
                    Context::new(DB::ForkDB(db), spec),
                    EthInstructions::new_mainnet_with_spec(spec),
                    EthPrecompiles{
                        precompiles: Precompiles::new(PrecompileSpecId::from_spec_id(spec)),
                        spec,
                    }
                );

                for account in slf_.accounts.iter() {
                    evm.db_mut().set_code(account.borrow(py).address.borrow(py).0, vec![])?;
                }

                evm.cfg.chain_id = chain_id.unwrap_or(forked_chain_id);
                evm.block.number = (forked_block.header.number + 1).try_into().unwrap();
                evm.block.timestamp = (forked_block.header.timestamp + 1).try_into().unwrap();
                slf_.evm = Some(EvmCell::new(evm));
                slf_.forked_chain_id = Some(forked_chain_id);
                slf_.forked_block = Some(forked_block.header.number);
            }
            None => {
                let db = CacheDB::new(EmptyDB::new(), 0);

                let mut evm: CustomEvm = Evm::new(
                    Context::new(DB::EmptyDB(db), spec),
                    EthInstructions::new_mainnet_with_spec(spec),
                    EthPrecompiles{
                        precompiles: Precompiles::new(PrecompileSpecId::from_spec_id(spec)),
                        spec,
                    }
                );
                evm.cfg.chain_id = chain_id.unwrap_or(31337);
                evm.block.number = U256::ZERO;
                evm.block.timestamp = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_secs()
                    .try_into()
                    .unwrap();
                slf_.evm = Some(EvmCell::new(evm));
            }
        }

        let block_gas_limit = slf_.block_gas_limit;
        // Collected before `get_evm_mut`, which borrows `slf_` mutably.
        let account_addresses: Vec<RevmAddress> = slf_
            .accounts
            .iter()
            .map(|account| account.borrow(py).address.borrow(py).0)
            .collect();
        let evm = slf_.get_evm_mut()?;
        evm.cfg.limit_contract_code_size = Some(usize::max_value());
        evm.cfg.disable_nonce_check = true;
        evm.cfg.disable_eip3607 = true;
        evm.block.gas_limit = block_gas_limit;
        // Pre-fund the development accounts, on both the empty and the forked db.
        // Written straight to the db rather than through `Account::set_balance`,
        // which mines a block and prunes history - that would add one block per
        // account before the chain is even usable.
        for address in account_addresses {
            evm.db_mut()
                .set_balance(address, U256::from(DEFAULT_ACCOUNT_BALANCE_WEI))?;
        }
        slf_.chain_id = evm.cfg.chain_id;

        slf_.blocks = Some(Py::new(py, Blocks::new(slf.clone_ref(py), slf_.forked_block))?);
        slf_.txs = Some(Py::new(py, Txs::new())?);

        let _ = slf_.mine(py, true)?; // mine one block

        Ok(())
    }

    fn _disconnect(slf: Bound<Self>, py: Python) -> PyResult<()> {
        let mut borrowed = slf.borrow_mut();
        borrowed.connected = false;

        if let Some(block_number) = borrowed.forked_block {
            let path = format!(
                ".wake/fork_cache/{}/{}",
                borrowed.forked_chain_id.unwrap(),
                block_number
            );
            match std::fs::create_dir_all(&path) {
                Ok(_) => {
                    let state_db_path = format!("{}/state.db", path);

                    if let Some(evm) = &mut borrowed.evm {
                        if let Err(e) = evm.db_mut().dump_forked_state(&state_db_path) {
                            log::warn!("Failed to dump forked state: {}", e);
                        }
                    }
                }
                Err(e) => {
                    log::warn!("Failed to create fork cache directory: {}", e);
                }
            }
        }

        let py_objects = get_py_objects(py);
        let connected_chains = py_objects.wake_connected_chains.bind(py);

        let index = connected_chains.index(slf.into_pyobject(py)?)?;
        connected_chains.del_item(index)?;

        Ok(())
    }

    #[pyo3(signature = (
        creation_code,
        *,
        request_type=RequestTypeEnum::Tx,
        return_tx=false,
        from_=None,
        value=ValueEnum::Int(BigUint::ZERO),
        gas_limit=None,
        gas_price=None,
        max_fee_per_gas=None,
        max_priority_fee_per_gas=None,
        access_list=None,
        authorization_list=None,
        block=None,
        confirmations=None,
        revert_on_failure=true,
        return_call=false,
    ))]
    fn deploy(
        slf: &Bound<Self>,
        py: Python,
        creation_code: Vec<u8>,
        request_type: RequestTypeEnum,
        return_tx: bool,
        from_: Option<AddressEnum>,
        value: ValueEnum,
        gas_limit: Option<GasLimitEnum>,
        gas_price: Option<ValueEnum>,
        max_fee_per_gas: Option<ValueEnum>,
        max_priority_fee_per_gas: Option<ValueEnum>,
        access_list: Option<AccessListEnum>,
        authorization_list: Option<Vec<Bound<'_, PyDict>>>,
        block: Option<BlockEnum>,
        confirmations: Option<u64>,
        revert_on_failure: bool,
        return_call: bool,
    ) -> PyResult<Py<PyAny>> {
        Contract::_execute(
            &Contract::type_object(py),
            py,
            slf.into_py_any(py)?,
            request_type,
            &hex::encode(creation_code),
            vec![],
            return_tx,
            Contract::type_object(py).into_any(),
            from_,
            None,
            value,
            gas_limit,
            gas_price,
            max_fee_per_gas,
            max_priority_fee_per_gas,
            access_list,
            authorization_list,
            block,
            confirmations,
            revert_on_failure,
            return_call,
        )
    }
}

impl Chain {
    fn with_evm_with_inspector<'a, F, R, I>(&mut self, py: Python, inspector: I, f: F) -> R
    where
        F: FnOnce(&mut Evm<CustomContext, I, EthInstructions<EthInterpreter, CustomContext>, EthPrecompiles, EthFrame<EthInterpreter>>) -> R + Send,
        R: Send,
        I: Send + 'a + InspectorExt<CustomContext>,
    {
        let evm = self.evm.take().expect("Not connected").into_inner().with_inspector(inspector);

        // The evm-with-inspector is also non-Send; use SendWrapper for allow_threads.
        let mut wrapped = SendWrapper::new(evm);

        let result = py.detach(|| f(&mut *wrapped));

        self.evm = Some(EvmCell::new(wrapped.take().with_inspector(())));

        result
    }

    pub(crate) fn get_evm(&self) -> PyResult<&EvmCell> {
        self.evm
            .as_ref()
            .ok_or_else(|| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("Not connected"))
    }

    pub(crate) fn get_evm_mut(&mut self) -> PyResult<&mut EvmCell> {
        self.evm
            .as_mut()
            .ok_or_else(|| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("Not connected"))
    }

    pub(crate) fn mine(&mut self, py: Python, force: bool) -> PyResult<Option<Py<Block>>> {
        if !self.automine && !force {
            return Ok(None);
        }

        let evm = self.evm
            .as_mut()
            .ok_or_else(|| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("Not connected"))?;
        self.latest_block_env = Some(evm.block.clone());

        let last_block_number = self.latest_block_env.as_ref().unwrap().number.try_into().unwrap();
        let block_hash = B256::from_slice(&self.rng.r#gen::<[u8; 32]>());

        evm.db_mut().set_last_block_number(last_block_number);
        evm.db_mut().set_block_hash(last_block_number, block_hash);

        let mut block_env = evm.block.clone();
        // reset to its original value
        block_env.gas_limit += self.pending_gas_used;

        let block = self
            .blocks
            .as_mut()
            .unwrap()
            .bind(py)
            .borrow_mut()
            .add_block(
                py,
                block_env,
                evm.db().journal_point(),
                block_hash,
                self.pending_gas_used,
            )?;

        // assign mined block to pending txs and clear them
        for tx in self.pending_txs.drain(..) {
            tx.borrow_mut(py).block = BlockInfo::Mined(block.clone_ref(py));
        }
        self.pending_gas_used = 0;

        // prepare pending block
        evm.block.number.add_assign(U256::from(1));
        evm.block.timestamp.add_assign(U256::from(1));
        evm.block.gas_limit = self.block_gas_limit;

        Ok(Some(block))
    }

    pub(crate) fn last_block_number(&self) -> PyResult<u64> {
        Ok(self.get_evm()?.db().last_block_number())
    }

    pub(crate) fn journal_index(&self) -> PyResult<usize> {
        Ok(self.get_evm()?.db().get_journal_index())
    }

    /// Lowest journal index any live pin still needs, i.e. how far back the
    /// journal must reach.
    ///
    /// O(1): every term is the front of a deque or a `Vec::first`, never a scan.
    ///
    /// Snapshots deliberately do not appear here. Reverting one restores state by
    /// truncating the copy-on-write layer stack without reading a single journal
    /// entry, so pinning the journal at a snapshot's index would buy nothing and
    /// cost everything — for a campaign snapshot taken at the start, the whole
    /// campaign.
    fn journal_floor(&self, py: Python, txs: &Txs, blocks: &Blocks) -> PyResult<usize> {
        // Seeding with the tip rather than `usize::MAX` means "no pins" compacts
        // every journal entry and keeps the floor from exceeding the tip. A
        // connected chain always retains at least its tip (`block_history = 0`
        // is normalized to 1).
        let mut floor = self.get_evm()?.db().get_journal_index();
        if let Some(index) = txs.oldest_journal_index() {
            floor = floor.min(index);
        }
        if let Some(index) = blocks.oldest_journal_index() {
            floor = floor.min(index);
        }
        // Transactions in the block being built are live by any definition, and
        // with automine off there can be more of them than the window holds, so
        // `txs.front()` is not a lower bound on its own.
        if let Some(tx) = self.pending_txs.first() {
            floor = floor.min(tx.borrow(py).journal_index.index);
        }
        Ok(floor)
    }

    /// Trims history back to the configured window and compacts the journal to
    /// match.
    ///
    /// **Blocks are the unit of retention.** Transactions are not counted
    /// separately: they are kept exactly as long as the block containing them is.
    /// That bounds retention in blocks, but not in bytes: a block's journal cost is
    /// proportional to the writes it contains, and the unmined block never closes,
    /// so with automine off retention plateaus only once blocks are mined.
    ///
    /// Called after sending a transaction and after mining, never during
    /// execution. Total work is O(items dropped) and every item is dropped
    /// exactly once, so how often this runs does not change throughput, only how
    /// the cost is distributed — which is why the hysteresis slack is small
    /// rather than a second full window: overshoot is paid in resident memory,
    /// and with fat blocks a 2x window is hundreds of megabytes, while draining
    /// eight times as often costs one extra length compare per drain.
    pub(crate) fn maybe_prune(slf: &Bound<Self>, py: Python) -> PyResult<()> {
        // Declared before the borrows so they are dropped after them: releasing
        // the last reference to a transaction can run `__del__`, which can
        // re-enter the chain, and re-entering a mutable borrow panics — crossing
        // pyo3 as `PanicException`, which slips past `except Exception`.
        let garbage_txs;
        let garbage_blocks;

        {
            let mut chain = slf.borrow_mut();
            // A configured window of 0 is normalized to 1 on connect so the tip
            // remains resolvable through `blocks["latest"]`.
            let Some(keep) = chain.block_history else {
                return Ok(());
            };

            let txs_cell = chain.txs.as_ref().expect("Not connected").clone_ref(py);
            let blocks_cell = chain.blocks.as_ref().expect("Not connected").clone_ref(py);
            let mut txs = txs_cell.borrow_mut(py);
            let mut blocks = blocks_cell.borrow_mut(py);

            // Let the window overshoot by an eighth before draining it back, so
            // the drain runs once per `keep / 8` blocks instead of once per block.
            let slack = keep.div_ceil(8).clamp(1, 32);
            if blocks.len() <= keep + slack {
                return Ok(());
            }

            // This is the only place the snapshots' windows can be lost, so it is
            // where they get frozen — after the hysteresis check above, so a
            // revert/replay loop that never prunes never pays for it.
            for snapshot in chain.snapshots.iter_mut() {
                snapshot.capture_window(py, &txs, &blocks);
            }

            let dropped_blocks = blocks.drain_front(keep);
            // The newest dropped block's journal index is the transaction cutoff:
            // a block's index is recorded after it and a transaction's before it,
            // so everything below it belonged to a dropped block. Transactions not
            // yet in a block sit above the newest block's index and are kept.
            garbage_txs = match dropped_blocks.last() {
                Some(newest_dropped) => txs.drain_below(newest_dropped.journal_index),
                None => Vec::new(),
            };
            garbage_blocks = dropped_blocks;

            let floor = chain.journal_floor(py, &txs, &blocks)?;
            let oldest_replayable_block = blocks.start_index() as u64;
            drop(txs);
            drop(blocks);

            // Every live snapshot can restore its captured block window. Preserve
            // the `BLOCKHASH` horizon of that whole window, not just its tip: empty
            // restored blocks can share a replayable journal point.
            let snapshot_ranges: Vec<(u64, u64)> = chain
                .snapshots
                .iter()
                .map(|snapshot| snapshot.block_hash_range())
                .collect();
            let db = chain.get_evm_mut()?.db_mut();
            db.compact_journal(floor, oldest_replayable_block);
            // The oldest retained block is the oldest thing a replay can execute
            // against, and a replay reads 256 blocks below its own block.
            db.prune_block_hashes(oldest_replayable_block, &snapshot_ranges);
        }

        drop(garbage_txs);
        drop(garbage_blocks);
        Ok(())
    }

    pub(crate) fn call(
        slf: &Bound<Self>,
        py: Python,
        data: Vec<u8>,
        to: Option<RevmAddress>,
        value: U256,
        from_: Option<AddressEnum>,
        gas_limit: Option<GasLimitEnum>,
        gas_price: Option<u128>,
        max_fee_per_gas: Option<U256>,
        max_priority_fee_per_gas: Option<U256>,
        access_list: Option<AccessListEnum>,
        authorization_list: Option<Vec<Bound<'_, PyDict>>>,
        block: BlockEnum,
        return_type: Option<Bound<PyAny>>,
        abi: Option<Bound<PyDict>>,
        return_call: bool,
    ) -> PyResult<Py<PyAny>> {
        let mut borrowed = slf.borrow_mut();
        let default_call_account = from_.unwrap_or(AddressEnum::Account(
            borrowed
                .default_call_account
                .as_ref()
                .expect("Default call account not set")
                .clone_ref(py),
        ));
        let collect_coverage = borrowed.collect_coverage;
        let evm = borrowed.get_evm()?;
        let current_journal_index = evm.db().journal_point();
        let block_gas_limit = evm.block.gas_limit;
        let tx_gas_limit_cap = evm.cfg.tx_gas_limit_cap();
        let tx_env = prepare_tx_env(
            py,
            borrowed.chain_id,
            block_gas_limit,
            tx_gas_limit_cap,
            data,
            to,
            value,
            default_call_account,
            gas_limit,
            gas_price,
            max_fee_per_gas,
            max_priority_fee_per_gas,
            access_list,
            authorization_list,
        )?;

        let py_objects = get_py_objects(py);

        let mut inspector: Box<dyn FqnInspectorExt<CustomContext>> = if !collect_coverage {
            Box::new(FqnInspector::new())
        } else {
            Box::new(CoverageInspector::new())
        };

        let (res, journal_index) = match block {
            BlockEnum::Pending => {
                let res = borrowed
                .with_evm_with_inspector(py, &mut *inspector, |evm| evm.inspect_tx(tx_env))
                .map_err(|e: EVMError<DBError>| PyErr::new::<PyRuntimeError, _>(e.to_string()))?;

                (res, current_journal_index)
            }
            BlockEnum::Int(_) | BlockEnum::Latest => {
                let block = borrowed.blocks.as_ref().unwrap().borrow_mut(py).get_block(
                    py,
                    block.clone(),
                    borrowed.last_block_number()?,
                    borrowed.provider.clone(),
                    borrowed.forked_chain_id,
                )?;
                let block = block.borrow(py);
                let journal_index = block.journal_index;
                let journal_index = if let Some(journal_index) = journal_index {
                    journal_index
                } else {
                    todo!() // fetch from forked chain wih rpc
                };
                let block_env = block.block_env.clone();

                let res = borrowed
                    .with_evm_with_inspector(py, &mut *inspector, |evm| {
                        let block_env_backup =
                            mem::replace(&mut evm.block, block_env);
                        let out = match evm.db_mut().rollback(journal_index) {
                            Ok(rollback) => {
                                let res = evm.inspect_tx(tx_env);
                                evm.db_mut().restore_rollback(rollback);
                                Ok(res)
                            }
                            Err(err) => Err(HistoryPrunedError::new_err(err.to_string())),
                        };
                        evm.block = block_env_backup;
                        out
                    })?
                    .map_err(|e: EVMError<DBError>| PyErr::new::<PyRuntimeError, _>(e.to_string()))?;

                (res, journal_index)
            }
            _ => return Err(PyValueError::new_err("Invalid block")),
        };

        inspector.sync_coverage(py)?;

        if !return_call && let ExecutionResult::Success { output, .. } = res.result {
            // happy fast path
            if let Some(abi) = abi && let Some(return_type) = return_type {
                Ok(decode_and_normalize(
                    py,
                    output.data(),
                    &abi,
                    &return_type,
                    &Py::from(borrowed),
                    intern!(py, "outputs"),
                    py_objects,
                )?)
            } else {
                PyBytes::new(py, output.data()).into_py_any(py)
            }
        } else {
            let evm = borrowed.get_evm()?;
            let block = match block {
                BlockEnum::Pending => BlockInfo::Pending(evm.block.clone()),
                BlockEnum::Int(_) | BlockEnum::Latest => {
                    let block = borrowed.blocks.as_ref().unwrap().borrow_mut(py).get_block(
                        py,
                        block,
                        borrowed.last_block_number()?,
                        borrowed.provider.clone(),
                        borrowed.forked_chain_id,
                    )?;
                    BlockInfo::Mined(block.clone_ref(py))
                },
                _ => return Err(PyValueError::new_err("Invalid block")),
            };
            // TODO: possibly dangerous: assumes that inspect_tx has set the tx_env and didn't change it
            let tx_env = evm.tx.clone();
            let call = Py::new(
                py,
                Call::new(
                    Py::from(borrowed),
                    block,
                    journal_index,
                    tx_env,
                    return_type.map(|r| Py::from(r)),
                    res.result,
                    abi.map(|abi| Py::from(abi)),
                    inspector.into_metadata().0,
                    None,
                )
            )?;
            match Call::error(call.bind(py), py)? {
                Some(error) => Err(error),
                None => Ok(call.into_any()),
            }
        }
    }

    pub(crate) fn transact(
        slf: &Bound<Self>,
        py: Python,
        data: Vec<u8>,
        to: Option<RevmAddress>,
        value: U256,
        from_: Option<AddressEnum>,
        gas_limit: Option<GasLimitEnum>,
        gas_price: Option<u128>,
        max_fee_per_gas: Option<U256>,
        max_priority_fee_per_gas: Option<U256>,
        access_list: Option<AccessListEnum>,
        authorization_list: Option<Vec<Bound<'_, PyDict>>>,
        return_type: Bound<PyAny>,
        abi: Option<Bound<PyDict>>,
    ) -> PyResult<Py<TransactionAbc>> {
        let mut borrowed = slf.borrow_mut();
        let default_tx_account = from_.unwrap_or(AddressEnum::Account(
            borrowed
                .default_tx_account
                .as_ref()
                .expect("Default tx account not set")
                .clone_ref(py),
        ));
        let collect_coverage = borrowed.collect_coverage;

        let evm = borrowed.get_evm()?;
        let block_gas_limit = evm.block.gas_limit;
        let tx_gas_limit_cap = evm.cfg.tx_gas_limit_cap();
        let tx_env = prepare_tx_env(
            py,
            borrowed.chain_id,
            block_gas_limit,
            tx_gas_limit_cap,
            data,
            to,
            value,
            default_tx_account,
            gas_limit,
            gas_price,
            max_fee_per_gas,
            max_priority_fee_per_gas,
            access_list,
            authorization_list,
        )?;

        let mut inspector: Box<dyn FqnInspectorExt<CustomContext>> = if !collect_coverage {
            Box::new(FqnInspector::new())
        } else {
            Box::new(CoverageInspector::new())
        };

        let evm = borrowed.get_evm()?;
        let journal_index = evm.db().journal_point();

        let result = borrowed
            .with_evm_with_inspector(py, &mut *inspector, |evm| evm.inspect_tx_commit(tx_env.clone()))
            .map_err(|e: EVMError<DBError>| PyErr::new::<PyRuntimeError, _>(e.to_string()))?;

        inspector.sync_coverage(py)?;

        let gas_limit_before = borrowed.get_evm()?.block.gas_limit;
        borrowed.get_evm_mut()?.block.gas_limit -= result.tx_gas_used();
        borrowed.pending_gas_used += result.tx_gas_used();

        let block = match borrowed.mine(py, false)? {
            Some(block) => BlockInfo::Mined(block),
            None => {
                BlockInfo::Pending(borrowed.get_evm()?.block.clone())
            }
        };
        let mined = matches!(block, BlockInfo::Mined(_));

        let tx_hash = B256::from_slice(&borrowed.rng.r#gen::<[u8; 32]>());
        let (errors_metadata, events_metadata) = inspector.into_metadata();
        let tx = Py::new(
            py,
            TransactionAbc::new(
                slf.clone().unbind(),
                block,
                Py::from(return_type),
                abi.map(|abi| Py::from(abi)),
                result,
                errors_metadata,
                events_metadata,
                journal_index,
                tx_env,
                gas_limit_before,
                tx_hash,
                borrowed.pending_txs.len() as u32,
            ),
        )?;
        if !mined {
            borrowed.pending_txs.push(tx.clone_ref(py));
        }

        borrowed
            .txs
            .as_mut()
            .unwrap()
            .bind(py)
            .borrow_mut()
            .add_tx(journal_index.index, tx.clone_ref(py));

        let tx_callback = borrowed
            .tx_callback
            .as_ref()
            .map(|tx_callback| tx_callback.clone_ref(py));

        drop(borrowed);

        // After the borrow is released: pruning drops Python objects, which can
        // run `__del__` and re-enter the chain.
        Chain::maybe_prune(slf, py)?;

        if let Some(tx_callback) = tx_callback {
            tx_callback.call1(py, (tx.clone_ref(py),))?;
        }

        match TransactionAbc::error(tx.bind(py), py)? {
            Some(error) => Err(error),
            None => Ok(tx),
        }
    }

    pub(crate) fn estimate(
        slf: &Bound<Self>,
        py: Python,
        data: Vec<u8>,
        to: Option<RevmAddress>,
        value: U256,
        from_: Option<AddressEnum>,
        gas_limit: Option<GasLimitEnum>,
        gas_price: Option<u128>,
        max_fee_per_gas: Option<U256>,
        max_priority_fee_per_gas: Option<U256>,
        access_list: Option<AccessListEnum>,
        authorization_list: Option<Vec<Bound<'_, PyDict>>>,
        block: BlockEnum,
        return_type: Option<Bound<PyAny>>,
        abi: Option<Bound<PyDict>>,
        revert: bool,
        return_call: bool,
    ) -> PyResult<Py<PyAny>> {
        let mut borrowed = slf.borrow_mut();
        let default_estimate_account = from_.unwrap_or(AddressEnum::Account(
            borrowed
                .default_estimate_account
                .as_ref()
                .expect("Default call account not set")
                .clone_ref(py),
        ));
        let evm = borrowed.get_evm()?;
        let current_journal_index = evm.db().journal_point();
        let block_gas_limit = evm.block.gas_limit;
        let tx_gas_limit_cap = evm.cfg.tx_gas_limit_cap();
        let tx_env = prepare_tx_env(
            py,
            borrowed.chain_id,
            block_gas_limit,
            tx_gas_limit_cap,
            data,
            to,
            value.try_into()?,
            default_estimate_account,
            gas_limit,
            gas_price,
            max_fee_per_gas,
            max_priority_fee_per_gas,
            access_list,
            authorization_list,
        )?;

        let mut inspector = FqnInspector::new();

        let (res, journal_index) = match block {
            BlockEnum::Pending => {
                let res = borrowed
                .with_evm_with_inspector(py, &mut inspector, |evm| evm.inspect_tx(tx_env))
                .map_err(|e| PyErr::new::<PyRuntimeError, _>(e.to_string()))?;

                (res, current_journal_index)
            }
            BlockEnum::Int(_) | BlockEnum::Latest => {
                let block = borrowed.blocks.as_ref().unwrap().borrow_mut(py).get_block(
                    py,
                    block.clone(),
                    borrowed.last_block_number()?,
                    borrowed.provider.clone(),
                    borrowed.forked_chain_id,
                )?;
                let block = block.borrow(py);
                let journal_index = block.journal_index;
                let journal_index = if let Some(journal_index) = journal_index {
                    journal_index
                } else {
                    todo!() // fetch from forked chain wih rpc
                };
                let block_env = block.block_env.clone();

                let res = borrowed
                    .with_evm_with_inspector(py, &mut inspector, |evm| {
                        let block_env_backup =
                            mem::replace(&mut evm.block, block_env);
                        let out = match evm.db_mut().rollback(journal_index) {
                            Ok(rollback) => {
                                let res = evm.inspect_tx(tx_env);
                                evm.db_mut().restore_rollback(rollback);
                                Ok(res)
                            }
                            Err(err) => Err(HistoryPrunedError::new_err(err.to_string())),
                        };
                        evm.block = block_env_backup;

                        out
                    })?
                    .map_err(|e| PyErr::new::<PyRuntimeError, _>(e.to_string()))?;

                (res, journal_index)
            }
            _ => return Err(PyValueError::new_err("Invalid block")),
        };

        if !return_call && (!revert || matches!(res.result, ExecutionResult::Success { .. })) {
            res.result.tx_gas_used().into_py_any(py)
        } else {
            let evm = borrowed.get_evm()?;
            let block = match block {
                BlockEnum::Pending => BlockInfo::Pending(evm.block.clone()),
                BlockEnum::Int(_) | BlockEnum::Latest => {
                    let block = borrowed.blocks.as_ref().unwrap().borrow_mut(py).get_block(
                        py,
                        block,
                        borrowed.last_block_number()?,
                        borrowed.provider.clone(),
                        borrowed.forked_chain_id,
                    )?;
                    BlockInfo::Mined(block.clone_ref(py))
                },
                _ => return Err(PyValueError::new_err("Invalid block")),
            };
            // TODO: possibly dangerous: assumes that inspect_tx has set the tx_env and didn't change it
            let tx_env = evm.tx.clone();
            let call = Py::new(
                py,
                Call::new(
                    Py::from(borrowed),
                    block,
                    journal_index,
                    tx_env,
                    return_type.map(|r| Py::from(r)),
                    res.result,
                    abi.map(|abi| Py::from(abi)),
                    inspector.into_errors_metadata(),
                    None,
                )
            )?;

            if revert && let Some(error) = Call::error(call.bind(py), py)? {
                Err(error)
            } else {
                Ok(call.into_any())
            }
        }
    }

    pub(crate) fn access_list(
        slf: &Bound<Self>,
        py: Python,
        data: Vec<u8>,
        to: Option<RevmAddress>,
        value: U256,
        from_: Option<AddressEnum>,
        gas_limit: Option<GasLimitEnum>,
        gas_price: Option<u128>,
        max_fee_per_gas: Option<U256>,
        max_priority_fee_per_gas: Option<U256>,
        authorization_list: Option<Vec<Bound<'_, PyDict>>>,
        block: BlockEnum,
        return_type: Option<Bound<PyAny>>,
        abi: Option<Bound<PyDict>>,
        revert: bool,
        return_call: bool,
    ) -> PyResult<Py<PyAny>> {
        let mut borrowed = slf.borrow_mut();
        let default_access_list_account = from_.unwrap_or(AddressEnum::Account(
            borrowed
                .default_access_list_account
                .as_ref()
                .expect("Default access list account not set")
                .clone_ref(py),
        ));
        let evm = borrowed.get_evm()?;
        let current_journal_index = evm.db().journal_point();
        let block_gas_limit = evm.block.gas_limit;
        let tx_gas_limit_cap = evm.cfg.tx_gas_limit_cap();
        let tx_env = prepare_tx_env(
            py,
            borrowed.chain_id,
            block_gas_limit,
            tx_gas_limit_cap,
            data,
            to,
            value.try_into()?,
            default_access_list_account,
            gas_limit,
            gas_price,
            max_fee_per_gas,
            max_priority_fee_per_gas,
            None,
            authorization_list,
        )?;

        let mut inspector = AccessListInspector::new(vec![].into());

        let (res, journal_index) = match block {
            BlockEnum::Pending => {
                let res = borrowed
                .with_evm_with_inspector(py, &mut inspector, |evm| evm.inspect_tx(tx_env))
                .map_err(|e| PyErr::new::<PyRuntimeError, _>(e.to_string()))?;

                (res, current_journal_index)
            }
            BlockEnum::Int(_) | BlockEnum::Latest => {
                let block = borrowed.blocks.as_ref().unwrap().borrow_mut(py).get_block(
                    py,
                    block.clone(),
                    borrowed.last_block_number()?,
                    borrowed.provider.clone(),
                    borrowed.forked_chain_id,
                )?;
                let block = block.borrow(py);
                let journal_index = block.journal_index;
                let journal_index = if let Some(journal_index) = journal_index {
                    journal_index
                } else {
                    todo!() // fetch from forked chain wih rpc
                };
                let block_env = block.block_env.clone();

                let res = borrowed
                    .with_evm_with_inspector(py, &mut inspector, |evm| {
                        let block_env_backup =
                            mem::replace(&mut evm.block, block_env);
                        let out = match evm.db_mut().rollback(journal_index) {
                            Ok(rollback) => {
                                let res = evm.inspect_tx(tx_env);
                                evm.db_mut().restore_rollback(rollback);
                                Ok(res)
                            }
                            Err(err) => Err(HistoryPrunedError::new_err(err.to_string())),
                        };
                        evm.block = block_env_backup;

                        out
                    })?
                    .map_err(|e| PyErr::new::<PyRuntimeError, _>(e.to_string()))?;

                (res, journal_index)
            }
            _ => return Err(PyValueError::new_err("Invalid block")),
        };

        if !return_call && (!revert || matches!(res.result, ExecutionResult::Success { .. })) {
            (access_list_into_py(inspector.into_access_list()), res.result.tx_gas_used()).into_py_any(py)
        } else {
            let evm = borrowed.get_evm()?;
            let block = match block {
                BlockEnum::Pending => BlockInfo::Pending(evm.block.clone()),
                BlockEnum::Int(_) | BlockEnum::Latest => {
                    let block = borrowed.blocks.as_ref().unwrap().borrow_mut(py).get_block(
                        py,
                        block,
                        borrowed.last_block_number()?,
                        borrowed.provider.clone(),
                        borrowed.forked_chain_id,
                    )?;
                    BlockInfo::Mined(block.clone_ref(py))
                },
                _ => return Err(PyValueError::new_err("Invalid block")),
            };
            // TODO: possibly dangerous: assumes that inspect_tx has set the tx_env and didn't change it
            let tx_env = evm.tx.clone();

            let AccessListInspector { inner, fqn_inspector } = inspector;
            let call = Py::new(
                py,
                Call::new(
                    Py::from(borrowed),
                    block,
                    journal_index,
                    tx_env,
                    return_type.map(|r| Py::from(r)),
                    res.result,
                    abi.map(|abi| Py::from(abi)),
                    fqn_inspector.into_errors_metadata(),
                    Some(inner.into_access_list()),
                )
            )?;
            if revert && let Some(error) = Call::error(call.bind(py), py)? {
                Err(error)
            } else {
                Ok(call.into_any())
            }
        }
    }

    /// Re-executes a transaction under `inspector` against the state it
    /// originally ran on.
    ///
    /// This is the only thing that needs the journal, which is why the retained
    /// window exists at all: `events`, `return_value`, `error`, `status` and
    /// `gas_used` all read the stored `ExecutionResult` and keep working however
    /// far back the transaction is.
    ///
    /// Replayability is checked here, at use, and never assumed: reverting a
    /// snapshot brings history from before the revert back into view, and those
    /// entries are legitimately gone. Erroring is the only honest answer — a
    /// rollback that stops short would replay against the wrong state and return
    /// a plausible, wrong trace.
    fn replay<I>(
        &mut self,
        py: Python,
        inspector: I,
        journal_index: JournalPoint,
        tx_env: &TxEnv,
        block_env: BlockEnv,
    ) -> PyResult<()>
    where
        I: Send + InspectorExt<CustomContext>,
    {
        let execution_block: u64 = block_env
            .number
            .try_into()
            .map_err(|_| PyRuntimeError::new_err("replay block number exceeds u64 range"))?;
        let oldest_replayable_block = self
            .blocks
            .as_ref()
            .expect("Not connected")
            .borrow(py)
            .start_index() as u64;
        if execution_block < oldest_replayable_block {
            return Err(HistoryPrunedError::new_err(format!(
                "block {execution_block} is no longer retained for replay because its block context was pruned: the retained window starts at block {oldest_replayable_block}. Raise `testing.block_history` in the configuration (or set it to null to disable pruning) to keep more."
            )));
        }

        self.with_evm_with_inspector(py, inspector, |evm| {
            let block_env_backup = mem::replace(&mut evm.block, block_env);
            let out = match evm.db_mut().rollback(journal_index) {
                Ok(rollback) => {
                    let _ = evm.inspect_tx(tx_env.clone());
                    evm.db_mut().restore_rollback(rollback);
                    Ok(())
                }
                Err(err) => Err(HistoryPrunedError::new_err(err.to_string())),
            };
            evm.block = block_env_backup;
            out
        })
    }

    pub(crate) fn get_call_trace(
        &mut self,
        py: Python,
        journal_index: JournalPoint,
        tx_env: &TxEnv,
        block_env: BlockEnv,
    ) -> PyResult<NativeTrace> {
        let mut inspector = TraceInspector::new();

        self.replay(py, &mut inspector, journal_index, tx_env, block_env)?;

        Ok(inspector.into_root_trace())
    }

    pub(crate) fn get_console_logs(
        &mut self,
        py: Python,
        journal_index: JournalPoint,
        tx_env: &TxEnv,
        block_env: BlockEnv,
    ) -> PyResult<Vec<Bytes>> {
        let mut inspector = ConsoleLogInspector::new();

        self.replay(py, &mut inspector, journal_index, tx_env, block_env)?;

        Ok(inspector.into_inputs())
    }

    pub(crate) fn get_access_list(
        &mut self,
        py: Python,
        journal_index: JournalPoint,
        tx_env: &TxEnv,
        block_env: BlockEnv,
    ) -> PyResult<AccessList> {
        let mut inspector = AccessListInspector::new(vec![].into());

        self.replay(py, &mut inspector, journal_index, tx_env, block_env)?;

        Ok(inspector.into_access_list())
    }
}

pub(crate) fn access_list_into_py(access_list: AccessList) -> HashMap<Address, Vec<BigUint>> {
    access_list
        .iter()
        .map(|a| {
            (
                Address::from(a.address),
                a.storage_keys
                    .iter()
                    .map(|k| BigUint::from_bytes_be(k.as_slice()))
                    .collect(),
            )
        })
        .collect()
}
