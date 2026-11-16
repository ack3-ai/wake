use std::collections::HashMap;

use revm::{
    context::{transaction::AccessList, ContextTr},
    inspector::JournalExt,
    interpreter::{CallInputs, CallOutcome, CreateInputs, CreateOutcome, Interpreter},
    primitives::Log,
    Inspector,
};
use revm_inspectors::access_list::AccessListInspector as RevmAccessListInspector;

use super::fqn_inspector::{ErrorMetadata, FqnInspector};

pub(crate) struct AccessListInspector {
    pub(crate) inner: RevmAccessListInspector,
    pub(crate) fqn_inspector: FqnInspector,
}

impl AccessListInspector {
    pub fn new(
        access_list: AccessList,
    ) -> Self {
        Self {
            inner: RevmAccessListInspector::new(access_list),
            fqn_inspector: FqnInspector::new(),
        }
    }

    pub fn into_access_list(self) -> AccessList {
        self.inner.into_access_list()
    }

    pub fn into_errors_metadata(self) -> HashMap<[u8; 4], ErrorMetadata> {
        self.fqn_inspector.into_errors_metadata()
    }
}

// both inspectors need every frame hook: `inner` collects the addresses excluded from the
// access list at the top-level frame, `fqn_inspector` tracks the executing bytecode
impl<CTX: ContextTr<Journal: JournalExt>> Inspector<CTX> for AccessListInspector {
    fn log_full(&mut self, interp: &mut Interpreter, context: &mut CTX, log: Log) {
        self.fqn_inspector.log_full(interp, context, log);
    }

    fn call(&mut self, context: &mut CTX, inputs: &mut CallInputs) -> Option<CallOutcome> {
        self.inner.call(context, inputs);
        self.fqn_inspector.call(context, inputs)
    }

    fn call_end(&mut self, context: &mut CTX, inputs: &CallInputs, outcome: &mut CallOutcome) {
        self.fqn_inspector.call_end(context, inputs, outcome)
    }

    fn create(&mut self, context: &mut CTX, inputs: &mut CreateInputs) -> Option<CreateOutcome> {
        self.inner.create(context, inputs);
        self.fqn_inspector.create(context, inputs)
    }

    fn create_end(&mut self, context: &mut CTX, inputs: &CreateInputs, outcome: &mut CreateOutcome) {
        self.fqn_inspector.create_end(context, inputs, outcome)
    }

    fn step(&mut self, interp: &mut Interpreter, context: &mut CTX) {
        self.inner.step(interp, context);
    }
}
