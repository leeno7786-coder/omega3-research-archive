# Dynamic Subdomain Surface Map — Exact File-by-File Mapping

> **Status:** MAPPING COMPLETE — read-only audit by Devin, 2026-06-14
> **Purpose:** Exact file/symbol/line mapping for making Y dynamic for lobes 2 (knowledge) and 8 (coding)
> **Scope:** 9 surfaces from OMEGA_FIRST_WAKE_DYNAMIC_SUBDOMAIN_PLAN.md section 4

---

## Surface 1: Taxonomy — `memory/dewey_map.py`

### (1) Exact files + symbols + line ranges
- **File:** `memory/dewey_map.py`
- **Symbols:**
  - `SUBDOMAIN_MAP` (lines 93-332)
  - `KNOWLEDGE_DOMAINS` (lines 337-502)
  - `CODING_DOMAINS` (lines 532-604)
  - `knowledge_coord()` function (lines 505-513)
  - `category_for_domain()` function (lines 604-774)
  - `SETFIT_LABELS` (implicit — derived from SUBDOMAIN_MAP for lobe 2/8)
  - `LOBE_MAP` (lines 27-40) — X routing unchanged

### (2) Current behavior
- `SUBDOMAIN_MAP` declares fixed `(X, Y) → dirname` mappings for all 12 lobes
- For lobe 2 (knowledge): 56 static subdomains (astronomy, physics, chemistry, biology, mathematics, etc.)
- For lobe 8 (coding): 50 static subdomains (python_general, python_async, typescript_general, rust_general, etc.)
- `KNOWLEDGE_DOMAINS` maps knowledge domain names → (Y_category, domain_dir) for third-tier routing
- `CODING_DOMAINS` maps coding domain names → (Y_subdomain, Z_cluster) with Z always 0 (static pattern)
- All coordinates are hard-coded; no dynamic allocation mechanism for Y

### (3) Coupling to static taxonomy
- **Direct coupling:** Every memory write uses `SUBDOMAIN_MAP[(X, Y)]` to resolve directory names
- **Classifier coupling:** SetFit models trained to predict Y values that exist in SUBDOMAIN_MAP
- **Index coupling:** `coord_index.json` stores coords with static Y values
- **Path resolution:** `memory/engine/validate_write.py:final_path_for_meta()` uses SUBDOMAIN_MAP directly (line 93)
- **Recall coupling:** `memory/recall_lanes.py` uses Y values for subdomain filtering (line 105-120)

### (4) Exact change required for dynamic Y (lobes 2 & 8)
1. **Retain X routing:** Keep `LOBE_MAP` and coarse `setfit_lobe` classifier (unchanged)
2. **Make Y lookup dynamic for X=2 and X=8:**
   - Add `DYNAMIC_Y_LOBES: set = {2, 8}` constant
   - Modify `SUBDOMAIN_MAP.get((X, Y))` logic: if X in DYNAMIC_Y_LOBES, skip static map and consult dynamic Y registry
   - Create new dynamic Y registry sidecar: `guardian/state/dynamic_y_index.json` with schema:
     ```json
     {
       "schema_version": 1,
       "updated_at": "ISO-8601",
       "lobes": {
         "2": {
           "next_y": 56,
           "subdomains": {
             "machine_learning": {"y": 56, "created_at": "..."},
             "react_patterns": {"y": 57, "created_at": "..."}
           }
         },
         "8": {
           "next_y": 50,
           "subdomains": {
             "kubernetes_helm": {"y": 50, "created_at": "..."},
             "rust_async_patterns": {"y": 51, "created_at": "..."}
           }
         }
       }
     }
     ```
3. **Add Y allocator function:** `allocate_dynamic_y(lobe_x: int, subdomain_name: str) -> int`
   - Checks if name exists in registry → return Y
   - Otherwise allocates next_y, increments counter, persists, returns new Y
4. **Keep static Y for other lobes:** X ∈ {0,1,3,4,5,6,7,9,10,11} use static SUBDOMAIN_MAP unchanged
5. **Deprecate SETFIT_LABELS for lobe 2/8:** The runtime classifier's label_map.json no longer needs Y labels for knowledge/coding subdomains

### (5) Risks
- **Coord collision risk:** If day-zero doesn't wipe old static Y coords, new dynamic Y may collide with historical records
- **Index corruption risk:** `coord_index.json` contains static Y values that won't resolve after dynamic transition
- **SetFit prediction drift:** Runtime classifier may still predict Y values that don't exist in dynamic registry
- **Path resolution failure:** `final_path_for_meta()` fallback logic (lines 128-135) assumes static KNOWLEDGE_DOMAINS
- **Recall scoping break:** `allowed_subdomains` filtering in `memory/api.py` (lines 347-353) assumes stable Y values

### (6) Tests affected
- `tests/memory/test_category_for_domain.py` — pins KNOWLEDGE_DOMAINS entries
- `tests/auto_loop/test_subdomain_recall.py` — tests subdomain-specific recall with static Y
- `tests/memory/test_ingest_subdomain_override.py` — tests Y override mechanics
- `tests/memory/test_memory_comprehensive.py` — may assert specific coord values
- `tests/memory/test_persona.py` — uses SUBDOMAIN_MAP for lobe 1 (unchanged but may need validation)
- `tests/core/test_runtime_tool_sockets.py` — references setfit_knowledge/setfit_coding slots

---

## Surface 2: Consumers — importers of taxonomy symbols

### (1) Exact files + symbols + line ranges
- **`memory/engine/validate_write.py`**
  - Imports: `from memory.dewey_map import LOBE_MAP, SUBDOMAIN_MAP, KNOWLEDGE_DOMAINS` (line 67)
  - Usage: `final_path_for_meta()` uses `SUBDOMAIN_MAP.get((X, Y))` (line 93)
  - Usage: Fallback to `KNOWLEDGE_DOMAINS.get(domain)` (line 131)
  
- **`memory/knowledge_router.py`**
  - Import: `from memory.dewey_map import SUBDOMAIN_MAP` (line 245)
  - Usage: `empty_subdomain_count()` iterates SUBDOMAIN_MAP for lobe 2 (lines 250-251)
  
- **`memory/engine/hierarchical_classifier.py`**
  - Import: `from memory.dewey_map import SUBDOMAIN_MAP` (line 63)
  - Usage: `_code_workflow_markers()` derives artifact/process lexicon from SUBDOMAIN_MAP (lines 72-77)
  
- **`memory/gates/lobe_gates.py`**
  - Implicit coupling via enrichment functions that assume static subdomain structure
  - `ToolsGate._SCOPE_TO_Y` maps scope → Y for lobe 5 (line 110) — pattern to mirror for dynamic allocator
  
- **`memory/recall_lanes.py`**
  - `_LANE_TO_LOBES` mapping includes subdomain filters for lane scoping (lines 105-120)
  - `_entry_subdomain()` extracts Y from coord (lines 133-140)
  
- **`memory/api.py`**
  - `QuerySpec.allowed_subdomains` filters candidates by Y (lines 95, 347-353)
  
- **`core/persona_router.py`**
  - Import: `from memory.dewey_map import SUBDOMAIN_MAP` (lines 58, 161, 163)
  - Usage: Validates Y allocations for lobe 1 (unchanged but may need parallel pattern)

### (2) Current behavior
- All consumers import SUBDOMAIN_MAP/KNOWLEDGE_DOMAINS as static dictionaries
- Path resolution assumes static `(X, Y) → dirname` lookup always succeeds
- Filtering operations assume stable Y values across cycles
- No fallback for missing Y — lookup failures would raise KeyError

### (3) Coupling to static taxonomy
- **Path construction:** `validate_write.py` resolves directories via static map lookup
- **Counting/aggregation:** `knowledge_router.empty_subdomain_count()` counts static entries
- **Lexicon derivation:** `hierarchical_classifier` derives vocabulary from static dir names
- **Lane scoping:** Recall lanes use hardcoded Y values for filtering (e.g., tools lane → Y=1)

### (4) Exact change required for dynamic Y (lobes 2 & 8)
1. **Add dynamic lookup helper in dewey_map.py:**
   ```python
   def resolve_subdomain_dir(x: int, y: int) -> str:
       """Resolve (X, Y) → dirname, handling dynamic Y for lobes 2 & 8."""
       if x in DYNAMIC_Y_LOBES:
           return _lookup_dynamic_y(x, y)  # Consult dynamic_y_index.json
       return SUBDOMAIN_MAP.get((x, y), f"{y:02d}")
   ```
2. **Replace direct SUBDOMAIN_MAP.get() calls:**
   - `validate_write.py:93` → use `resolve_subdomain_dir(X, Y)`
   - Keep KNOWLEDGE_DOMAINS fallback for legacy domain field (third-tier Z, not Y)
3. **Update knowledge_router.empty_subdomain_count():**
   - Count dynamic Y entries from dynamic_y_index.json for lobe 2
   - Keep static count for other lobes
4. **Update hierarchical_classifier lexicon derivation:**
   - For lobe 8/10, derive markers from both static SUBDOMAIN_MAP AND dynamic registry
   - This ensures new coding subdomains participate in 8↔10 disambiguation
5. **Update recall_lanes.py:**
   - No change needed — lane filtering works on numeric Y, dynamic Y still numeric
   - Consider adding dynamic Y enumeration for diagnostic lanes

### (5) Risks
- **Import churn:** Many files import these symbols; changes need careful coordination
- **Fallback path complexity:** Static vs dynamic lookup adds branching logic that must be bulletproof
- **Lexicon staleness:** Coding/workflow disambiguation lexicon won't include new dynamic subdomains until registry reload
- **Count logic mismatch:** empty_subdomain_count() may report different counts after transition

### (6) Tests affected
- All tests that write to memory and assert file paths
- Tests that count subdomains or enumerate directory structures
- `tests/runtime/test_routing_coverage.py` — validates routing coverage across subdomains
- `tests/memory/test_memory_supersedes.py` — may assert coord structures
- Any test that mocks SUBDOMAIN_MAP directly

---

## Surface 3: Write/routing path — memory write and Y allocation

### (1) Exact files + symbols + line ranges
- **`memory/api.py`**
  - `write_memory()` function (lines 515-643)
  - `CanonicalEnvelope` dataclass (lines 147-169) — carries coord
  - `_envelope_to_dict()` helper (lines 232-249)
  
- **`memory/gates/base.py`**
  - `MemoryGate.ingest()` method (lines 101-200)
  - `_classify()` method (lines 200-250) — calls hierarchical classifier
  - `classify_within_lobe()` method (lines 250-280)
  
- **`memory/gates/lobe_gates.py`**
  - `KnowledgeGate` class (lines 54-59)
  - `ToolsGate.ingest()` override (lines 112-150) — pattern for deterministic Y override
  
- **`memory/gates/subdomain_override.py`**
  - `apply_subdomain_override()` function — allows manual Y override
  
- **`memory/engine/hierarchical_classifier.py`**
  - `HierarchicalClassifier.classify()` method (lines 150-200)
  - `_adapt_to_tuple()` method (lines 200-250)
  - `_disambiguate_code_workflow()` method (lines 101-124)
  
- **`memory/engine/validate_write.py`**
  - `final_path_for_meta()` function (lines 81-150) — constructs disk path from coord
  - `atomic_write()` function (lines 150-250)

### (2) Current behavior
1. Write flow: `gate.ingest(text)` → classifier.classify() → returns (X, Y, Z)
2. Classifier predicts X (lobe) via setfit_lobe, then Y (subdomain) via setfit_knowledge/setfit_coding
3. Gate enriches metadata, builds canonical envelope with coord
4. `validate_write.atomic_write()` resolves path via `final_path_for_meta()`
5. Path resolution uses static SUBDOMAIN_MAP lookup for Y → dirname
6. For lobe 2, `knowledge_router.route_knowledge_content()` allocates Z dynamically on first write
7. For lobe 8, Z is always 0 (no dynamic allocation yet)

### (3) Coupling to static taxonomy
- **Classifier prediction:** SetFit models predict Y values from static training set
- **No Y allocation hook:** There's no seam to allocate new Y values — classifier must predict existing Y
- **Path resolution assumes existence:** `final_path_for_meta()` fails if Y not in SUBDOMAIN_MAP
- **Knowledge router pattern:** Z allocation exists for lobe 2, but no analogous Y allocator

### (4) Exact change required for dynamic Y (lobes 2 & 8)
1. **Add subdomain allocator (lift knowledge_router pattern one level up):**
   - New file: `memory/subdomain_router.py` (mirrors `knowledge_router.py`)
   - Function: `allocate_subdomain_y(lobe_x: int, subdomain_name: str) -> int`
   - Registry: `guardian/state/dynamic_y_index.json`
   - Logic: name exists → return Y; else allocate next_y, mkdir, persist
2. **Insert allocator into write path:**
   - After classifier returns (X, Y), check if X ∈ {2, 8}
   - If classifier predicted Y for dynamic lobe:
     - Derive subdomain_name from text/title (same pattern as knowledge_router._derive_name)
     - Call `allocate_subdomain_y(X, subdomain_name)` to get/allocate Y
     - Replace classifier's Y with allocator's Y
   - If classifier returned NO_TOPIC or fallback, still derive name and allocate
3. **Update gate.ingest() for dynamic lobes:**
   - For lobe 2/8 gates, add Y allocation step before enrichment
   - Preserve classifier's X prediction (coarse routing still needed)
4. **Retire subdomain SetFit for lobes 2 & 8:**
   - Runtime config: remove setfit_knowledge/setfit_coding slots for lobe 2/8
   - Keep setfit_lobe for X prediction
   - Fallback: when subdomain SetFit unavailable, derive name directly

### (5) Risks
- **Allocation race condition:** Concurrent writes to same new subdomain may race on next_y (needs lock like knowledge_router)
- **Name collision:** Different content may derive same name (e.g., "python" appears in many contexts)
- **Classifier override complexity:** Overriding classifier's Y prediction breaks the "classifier = authority" doctrine
- **Mkdir permissions:** Allocator must create directories under memory/200_knowledge/ and memory/800_coding/
- **Boot ordering:** Allocator must initialize before first write to dynamic lobes

### (6) Tests affected
- All ingest tests for knowledge and coding lobes
- `tests/memory/test_ingest_subdomain_override.py` — override logic may conflict with allocator
- Tests that mock classifier predictions
- `tests/auto_loop/test_subdomain_recall.py` — assumes static subdomain structure

---

## Surface 4: SetFit classifiers — brain invocation + runtime serving

### (1) Exact files + symbols + line ranges
- **`config/runtime.yaml`**
  - `runtime.services.classify.slots` (lines 30-36)
    - `lobe: setfit_lobe`
    - `knowledge: setfit_knowledge`
    - `coding: setfit_coding`
  
- **`memory/engine/hierarchical_classifier.py`**
  - `HierarchicalClassifier` class (lines 136-408)
  - `classify()` method (lines 150-200)
  - HTTP client to runtime: `self._client.classify()` (line 190)
  
- **`memory/engine/classifier_cache.py`** (inferred from imports)
  - Singleton pattern for classifier instance sharing
  
- **`memory/gates/base.py`**
  - `_get_classifier()` method (lines 76-110) — lazy-loads classifier singleton
  - Thread-safe double-checked locking pattern
  
- **`runtime/client.py`** (inferred)
  - `ClassifyResult` dataclass
  - `classify()` HTTP method
  
- **`memory/indexes/label_map.json`** (inferred from docs)
  - Static label → coordinate mapping for SetFit models

### (2) Current behavior
- Brain loads `HierarchicalClassifier` (HTTP client to omega-runtime)
- Runtime serves 3 SetFit models: setfit_lobe (X), setfit_knowledge (Y for lobe 2), setfit_coding (Y for lobe 8)
- Classifier calls runtime `/classify` endpoint with text and optional force_lobe
- Runtime returns (label_id, coord, confidence, tokens)
- Coord includes X (lobe) and Y (subdomain) predictions
- For lobe 2, Z is 0 (allocated later by knowledge_router)
- For lobe 8, Z is 0 (no Z allocation yet)

### (3) Coupling to static taxonomy
- **Training data:** SetFit models trained on static subdomain labels from SUBDOMAIN_MAP
- **Label space:** Runtime's label_map.json encodes static (X, Y) pairs
- **Prediction contract:** Classifier always predicts Y from its training label space
- **No unknown label handling:** Runtime rejects or fallbacks for labels not in training set

### (4) Exact change required for dynamic Y (lobes 2 & 8)
1. **Retire setfit_knowledge and setfit_coding:**
   - Update `config/runtime.yaml`: remove knowledge and coding slots
   - Keep lobe slot for X prediction
   - Add new optional slot: `subdomain_general` for generic subdomain prediction (optional)
2. **Update hierarchical_classifier:**
   - When X=2 or X=8, skip subdomain classifier call
   - Return coord with Y=0 (placeholder) or derive name-based Y directly
   - Add fallback mode: if subdomain classifier unavailable, use name derivation
3. **Runtime side changes (separate system):**
   - Unload setfit_knowledge/setfit_coding models from memory
   - Keep setfit_lobe for coarse X routing
   - Update label_map.json to remove lobe-2/8 subdomain labels
4. **Name derivation as subdomain signal:**
   - Add `derive_subdomain_name(text, title, lobe_x)` function
   - Use same tokenization/stopping logic as knowledge_router._derive_name
   - This replaces SetFit's Y prediction for lobes 2 & 8

### (5) Risks
- **Classification quality loss:** Removing subdomain SetFit may reduce routing accuracy
- **Runtime coordination:** Runtime config changes must sync with brain code changes
- **Fallback complexity:** Name derivation is deterministic but may be less semantic than SetFit
- **Label map staleness:** If label_map.json isn't updated, runtime may still serve old models
- **Boot failure:** If runtime doesn't support new config, brain classifier fails to initialize

### (6) Tests affected
- `tests/core/test_runtime_tool_sockets.py` — references setfit_knowledge/setfit_coding slots
- Tests that mock classifier predictions with specific Y values
- `tests/auto_loop/test_subdomain_recall.py` — relies on SetFit-based routing
- Integration tests that verify end-to-end classification accuracy

---

## Surface 5: Recall/read/index — does recall assume fixed subdomain set?

### (1) Exact files + symbols + line ranges
- **`memory/recall_lanes.py`**
  - `LANE_KNOWLEDGE` constant (line 68)
  - `LANE_CODING` constant (line 73)
  - `_LANE_TO_LOBES` mapping (lines 105-120)
  - `_entry_subdomain()` helper (lines 133-140)
  
- **`memory/recall_compiler.py`**
  - `compile_recall_plan()` function — builds lane weights
  - May use Y values for scoping
  
- **`memory/recall_posture.py`**
  - `POSTURE_CATALOG` — lane weight configurations
  - References knowledge/coding lanes with subdomain filters (lines 424-442)
  
- **`memory/answerability_index.py`** (inferred from plan)
  - May index by subdomain for answerability scoring
  
- **`memory/engine/walker.py`**
  - `walker_query()` function — walks coord_index
  - Loads metadata via `load_meta_by_path()`
  - No explicit subdomain assumptions in walker core
  
- **`memory/indexes/coord_index.json`** (runtime)
  - Stores coord for every memory: [X, Y, Z]
  - Static Y values from historical writes

### (2) Current behavior
- Recall lanes scope by lobe X (e.g., knowledge lane → lobe 2)
- Optional subdomain filtering via `allowed_subdomains` in QuerySpec
- Walker loads all candidates in lobe, then filters by Y if specified
- Answerability index may aggregate by subdomain
- Coord index stores whatever Y was written — static or future dynamic

### (3) Coupling to static taxonomy
- **Lane scoping assumes existence:** Filters like `allowed_subdomains=[0, 1, 2]` assume those Y exist
- **Answerability aggregation:** May assume fixed set of subdomains for scoring
- **Index compatibility:** Historical records with static Y must remain readable after dynamic transition
- **No dynamic enumeration:** Recall doesn't discover new subdomains; it filters known Y

### (4) Exact change required for dynamic Y (lobes 2 & 8)
1. **Verify recall compatibility:**
   - Walker works on numeric Y regardless of source (static or dynamic)
   - No change needed to core recall path — it's Y-agnostic
2. **Update lane filters:**
   - `allowed_subdomains` filtering works with dynamic Y (no change)
   - Consider adding "allow all subdomains in lobe" mode (wildcard)
3. **Update answerability index:**
   - If it aggregates by static subdomain list, change to aggregate by dynamic registry
   - Load dynamic Y entries from dynamic_y_index.json
4. **Coord index backward compatibility:**
   - Historical records with old static Y must remain readable
   - Walker doesn't care about Y source, only numeric value
   - No migration needed — old coords just "are what they are"
5. **Add dynamic subdomain enumeration:**
   - Helper: `list_subdomains_for_lobe(lobe_x: int) -> List[int]`
   - Consults static SUBDOMAIN_MAP for other lobes, dynamic registry for 2/8

### (5) Risks
- **Historical record isolation:** Old static Y values may not map to current dynamic Y names
- **Answerability skew:** If index assumes static subdomain set, new subdomains won't be indexed
- **Filter staleness:** Hardcoded `allowed_subdomains` lists won't include new dynamic Y
- **Index size growth:** Dynamic Y may proliferate subdomains, affecting recall performance

### (6) Tests affected
- `tests/memory/test_recall_posture_code_gate.py` — tests lane filtering
- `tests/auto_loop/test_subdomain_recall.py` — tests subdomain-scoped recall
- Any test that uses `allowed_subdomains` with specific Y values
- Integration tests that verify answerability across subdomains

---

## Surface 6: day_zero — scripts/day_zero_reset.py

### (1) Exact files + symbols + line ranges
- **`scripts/day_zero_reset.py`**
  - `_canonical_subdirs_for_lobe()` function (lines 607-630)
    - Imports SUBDOMAIN_MAP (line 619)
    - Returns canonical subdomains for a lobe
  - `normalize_lobe_structure()` function (lines 644-752)
    - Drops non-canonical subdirs (lines 709-713)
    - Creates missing canonical dirs (lines 715-723)
    - For lobe 2: drops third-level dirs (lines 743-751)
  - `LOBE_DIRS` constant (lines 65-78)
  - `FACTORY_PRESERVE` constant (lines 98-103) — preserves label_map.json

### (2) Current behavior
- Day-zero wipes all memory files but keeps directory structure
- `normalize_lobe_structure()` collapses tree to canonical schema
- For lobe 2: creates all 56 static knowledge subdomain dirs
- For lobe 8: creates all 50 static coding subdomain dirs
- Subdomain dirs are created empty (third-level Z allocated dynamically)
- `label_map.json` is preserved (static classifier scaffold)

### (3) Coupling to static taxonomy
- **Canonical set = static:** `_canonical_subdirs_for_lobe()` returns SUBDOMAIN_MAP entries
- **Museum effect:** After reset, tree reflects every historical static subdomain
- **No dynamic dirs:** Day-zero never creates operator-seeded subdomains
- **Empty = spark:** Empty subdomains at boot are doctrinally correct for curiosity

### (4) Exact change required for dynamic Y (lobes 2 & 8)
1. **Modify `_canonical_subdirs_for_lobe()` for dynamic lobes:**
   - For X ∈ {2, 8}, return empty dict (no canonical subdomains)
   - For other lobes, keep static SUBDOMAIN_MAP behavior
2. **Update `normalize_lobe_structure()` for dynamic lobes:**
   - For lobe 2/8: skip subdomain creation entirely
   - Let allocator create subdomain dirs on first write (onboarding)
   - Remove lines 743-751 (third-level dir dropping) — still needed for Z, not Y
3. **Preserve dynamic Y registry:**
   - Add `reset_dynamic_y_index()` function
   - Wipes `guardian/state/dynamic_y_index.json` to clean slate
   - Call this in main reset sequence
4. **Label map handling:**
   - Decide: preserve label_map.json (may contain stale lobe-2/8 labels) or wipe
   - Recommend: preserve for other lobes, but document lobe-2/8 labels as retired
5. **Operator onboarding creates first subdomains:**
   - Day-zero leaves lobes 2 & 8 with NO subdomain dirs
   - First operator profile SAVE creates initial Y subdomains

### (5) Risks
- **Boot failure risk:** If lobes 2/8 have no subdomain dirs, classifier may fail
- **Historical record loss:** Old memories with static Y won't have dirs after reset
- **Registry desync:** If dynamic_y_index.json isn't reset, stale Y allocations persist
- **Onboarding dependency:** System unusable until operator completes profile (first subdomain seed)
- **Curiosity engine breakage:** `empty_subdomain_count()` returns 0 (no subdomains exist)

### (6) Tests affected
- `tests/scripts/test_day_zero_*.py` — any day-zero reset tests
- Tests that assume specific subdirectory structure exists after reset
- Tests that verify canonical tree structure
- `tests/memory/test_knowledge_router.py` — expects subdomain dirs to exist

---

## Surface 7: HUD — hud/omega_interface.html + hud/server.py + hud/settings.py

### (1) Exact files + symbols + line ranges
- **`hud/omega_interface.html`**
  - Profile form fields (lines ~7000-8000, approximate)
  - Current fields: NAME, PREFERRED, BIRTHDAY, PRONOUNS, LOCATION, TIMEZONE, ROLE, WORKING ON NOW, TONE, SCHEDULE, NOTES
  - Form POST handler in JavaScript
  
- **`hud/settings.py`**
  - `OPERATOR_PROFILE_FIELDS` tuple (lines 89-102)
    - Current: role, working_on_now, tone_preference, schedule_pattern
  - `save_operator_profile()` function (lines ~400-500, inferred)
  - `build_operator_profile_envelope()` function (lines ~300-400, inferred)
  
- **`hud/server.py`**
  - Profile POST route (lines ~2700-2800, approximate)
  - Route handler calls `hud.settings.save_operator_profile()`

### (2) Current behavior
- HUD operator profile has free-text "role" field
- Profile SAVE writes to memory/100_personas/operator/
- No subdomain seeding happens on profile save
- Profile fields are cosmetic/organizational, not structural

### (3) Coupling to static taxonomy
- **No direct coupling:** HUD doesn't touch memory/200_knowledge/ or memory/800_coding/
- **Indirect coupling:** Profile "role" could be used to seed subdomains, but isn't today
- **Missing integration:** No bridge between operator profile and memory structure

### (4) Exact change required for dynamic Y (lobes 2 & 8)
1. **Redesign operator profile form:**
   - **ROLE → OCCUPATION:** `<select>` dropdown with ~15-25 standard occupations
   - **REMOVE:** WORKING ON NOW, TONE, SCHEDULE (per plan)
   - **ADD:** SUBCATEGORY selector — occupation-dependent multi-select with free-text add
   - **KEEP:** name, preferred, birthday, pronouns, location, timezone, notes
2. **Add occupation/subcategory vocabulary:**
   - New constant in `hud/settings.py`: `OCCUPATION_CATEGORIES`
   - Structure: `{occupation: [subcategory1, subcategory2, ...]}`
   - Example: `{"software_engineer": ["embedded", "ML", "web"], "teacher": ["science", "math", "art"]}`
3. **Wiring SAVE to allocator:**
   - On profile SAVE, for each chosen subcategory:
     - Determine lobe: knowledge vs coding (need occupation → lobe mapping)
     - Call `allocate_subdomain_y(lobe_x, subcategory_name)` to create Y
     - Plant starter learning topic in that subdomain
   - Update `save_operator_profile()` to call allocator
4. **Server route changes:**
   - Update POST handler to accept occupation/subcategory payload
   - Validate against occupation vocabulary
   - Call allocator before writing persona record

### (5) Risks
- **Occupation taxonomy complexity:** Designing standard occupation/subcategory vocabulary is nontrivial
- **Lobe assignment ambiguity:** Does "teacher" seed knowledge only? "software engineer" seed coding only? Some seed both?
- **Form UX complexity:** Multi-select with free-text add is complex to implement and validate
- **Allocation failure:** If allocator fails during profile SAVE, persona write may be incomplete
- **Starter topic seeding:** Need to define what "starter learning topic" means (empty cluster vs seeded content)

### (6) Tests affected
- HUD integration tests (if they exist)
- Tests that verify operator profile persistence
- Tests that mock HUD POST routes
- End-to-end tests that simulate operator onboarding flow

---

## Surface 8: Topic resolution — cognition/topic_resolution/resolve_topic.py

### (1) Exact files + symbols + line ranges
- **`cognition/topic_resolution/resolve_topic.py`**
  - `resolve_learning_topic()` function (lines 534-551)
  - `NO_TOPIC_AVAILABLE` constant (line 21)
  - Resolver cascade (lines 536-543):
    - `_resolve_operator_query()` (lines 135-145)
    - `_resolve_mastery_gap()` (lines 200-226)
    - `_resolve_trajectory()` (lines 494-522)
    - `_resolve_open_question()` (lines 148-182)
    - `_resolve_learning_debt()` (lines 248-268)
    - `_resolve_pressure_meta()` (lines 525-531)
  - `_is_concrete_topic()` helper (lines 52-54)

- **`cognition/topic_resolution/domain_key.py`**
  - `canonical_domain_key()` function — maps topic to domain key
  - `extract_topic_from_text()` function

### (2) Current behavior
- Topic resolution runs 6-source priority cascade to pick a learning topic
- Returns `NO_TOPIC_AVAILABLE` when all resolvers fail
- Empty lobes 2/8 cause every episode to resolve to `epistemic_emptiness`
- No bridge between subdomain structure and topic selection
- Domain resolution uses `canonical_domain_key()` which may assume static domains

### (3) Coupling to static taxonomy
- **Empty domain detection:** When lobes 2/8 are empty, no topics resolve
- **Domain key mapping:** `canonical_domain_key()` may map to static KNOWLEDGE_DOMAINS
- **No subdomain awareness:** Topic resolution doesn't know which subdomains exist
- **Mastery gap coupling:** `_resolve_mastery_gap()` reads mastery store which may use static domain IDs

### (4) Exact change required for dynamic Y (lobes 2 & 8)
1. **Bridge seeded subdomain → resolvable topic:**
   - After onboarding creates subdomains, they're empty (no content)
   - Add "seeded subdomain" resolver to cascade
   - Logic: if trajectory.target_domain matches a seeded subdomain, return that topic
   - Priority: after operator_query, before mastery_gap
2. **Empty cluster as valid topic:**
   - Change doctrine: empty subdomain = valid learning topic (not a gap)
   - `_is_concrete_topic()` should accept seeded subdomain names even if empty
3. **Domain key compatibility:**
   - Ensure `canonical_domain_key()` works with dynamic subdomain names
   - May need to map subdomain_name → domain_key for recall scoping
4. **Trajectory targeting:**
   - Trajectory `target_domain` should be set to operator's occupation/subcategory
   - This bridges onboarding to topic selection
5. **Mastery gap integration:**
   - Mastery store may need to handle dynamic subdomain IDs
   - Or: mastery operates at domain level, not subdomain level (need clarification)

### (5) Risks
- **Cascade priority complexity:** Adding new resolver changes priority ordering
- **Empty topic semantics:** Is "machine learning" a valid topic when the cluster has no content?
- **Trajectory coupling:** Trajectory must be set correctly for bridge to work
- **Domain mapping ambiguity:** How do dynamic subdomain names map to recall domain keys?
- **Mastery store schema:** If mastery uses static domain IDs, dynamic subdomains break it

### (6) Tests affected
- `tests/cognition/test_topic_resolution*.py` (if they exist)
- Tests that verify topic selection priority cascade
- Tests that mock mastery gap data
- Integration tests that verify LEARN intent resolves to concrete topics

---

## Surface 9: Tests — everything pinning the static taxonomy

### (1) Exact files + symbols + line ranges
- **`tests/memory/test_category_for_domain.py`**
  - Tests `category_for_domain()` function from dewey_map
  - Pins specific KNOWLEDGE_DOMAINS entries (lines 14, 51, 53, 58)
  
- **`tests/auto_loop/test_subdomain_recall.py`**
  - Tests subdomain-specific recall
  - Uses static Y values in allowed_subdomains (lines 39, 45, 46, 52)
  
- **`tests/memory/test_ingest_subdomain_override.py`**
  - Tests Y override mechanism
  - May assert specific Y values
  
- **`tests/memory/test_memory_comprehensive.py`**
  - May assert specific coord structures
  - Lines 323, 689, 693 reference SUBDOMAIN_MAP
  
- **`tests/memory/test_persona.py`**
  - Lines 60, 259 reference SUBDOMAIN_MAP (for lobe 1, unchanged but may need validation)
  
- **`tests/core/test_runtime_tool_sockets.py`**
  - Lines 43, 44, 45 reference setfit_knowledge/setfit_coding slots
  
- **`tests/memory/test_memory_supersedes.py`**
  - Line 423 may reference subdomain structure
  
- **`tests/memory/test_recall_posture_code_gate.py`**
  - Lines 15, 67, 68 reference subdomain filtering

### (2) Current behavior
- Tests pin specific subdomain Y values from static SUBDOMAIN_MAP
- Tests assert specific directory structures exist
- Tests mock classifier predictions with static Y values
- Tests verify routing to specific static subdomains

### (3) Coupling to static taxonomy
- **Hardcoded Y values:** Tests use `allowed_subdomains=[0, 1, 2]` assuming those exist
- **Directory assertions:** Tests expect `memory/200_knowledge/physics/` to exist
- **Classifier mocks:** Mock predictions return static Y values
- **Coord validation:** Tests assert coords like `[2, 24, 0]` (AI subdomain)

### (4) Exact change required for dynamic Y (lobes 2 & 8)
1. **Replace static Y assertions with dynamic checks:**
   - Instead of asserting Y=24 (AI), query allocator for "ai" subdomain's Y
   - Use allocator in test setup to create test subdomains
   - Assert on subdomain NAME, not numeric Y value
2. **Update classifier mocks:**
   - Mock classifier to return placeholder Y=0 for lobes 2/8
   - Let allocator override Y in test flow
   - Or mock allocator directly for deterministic test behavior
3. **Directory structure tests:**
   - Change assertions from "physics dir exists" to "allocator-created dir exists"
   - Use allocator in test setup to create expected dirs
4. **Subdomain recall tests:**
   - Change from `allowed_subdomains=[0, 1, 2]` to dynamic Y query
   - Test both: static subdomains (other lobes) and dynamic subdomains (lobes 2/8)
5. **Add new integration test:**
   - Day-zero → onboarding → first LEARN resolves to seeded topic
   - This is the canonical verification test for the whole feature

### (5) Risks
- **Test fragility:** Tests depending on allocator state may be flaky
- **Mock complexity:** Need to mock both classifier AND allocator for deterministic tests
- **Setup overhead:** Every test now needs allocator setup/teardown
- **Backward compatibility:** Old tests that assume static structure will fail
- **Coverage gaps:** New dynamic paths may lack test coverage initially

### (6) Tests affected
- ALL memory ingest tests for lobes 2 and 8
- ALL recall tests that use subdomain filtering
- ALL classifier tests that predict Y for lobes 2/8
- ALL day-zero reset tests that verify directory structure
- Integration tests for operator onboarding flow

---

## COMPLETENESS CRITIQUE

### Couplings the per-surface pass might miss:

1. **Enrichment modules** — `memory/enrichment/` directory
   - `enrich_knowledge()` may assume static subdomain structure
   - `enrich_coding()` may use static CODING_DOMAINS
   - `signal_detectors.py` may filter by static subdomain lists
   - **Risk:** Enrichment fails or produces wrong metadata for dynamic subdomains

2. **Coord index generation** — `memory/indexes/coord_index.json`
   - Generated dynamically during writes
   - Contains historical static Y values
   - **Risk:** Index contains mixed static/dynamic Y after transition; no migration path

3. **Label map runtime serving** — `memory/indexes/label_map.json`
   - Shipped with SetFit training labels
   - Runtime uses this for classifier label space
   - **Risk:** Runtime still serves stale lobe-2/8 subdomain labels after retirement

4. **Web sources configuration** — `config/web_sources.yaml`
   - Line 36 may reference static knowledge domains for credible source selection
   - **Risk:** Credible source routing breaks for new dynamic subdomains

5. **Persona router** — `core/persona_router.py`
   - Lines 58, 161, 163 import SUBDOMAIN_MAP
   - Uses Y allocation pattern for lobe 1 (personas)
   - **Risk:** Lobe 1 pattern may need to mirror dynamic allocator for consistency

6. **Coding project enrichment** — `memory/coding_project.py`
   - Lines 24, 615, 616 reference CODING_DOMAINS
   - **Risk:** Coding projects may fail to route to new dynamic subdomains

7. **HUD JS validation** — `hud/omega_interface.html` JavaScript
   - Client-side validation may assume static form fields
   - **Risk:** Occupation/subcategory form validation breaks new UI

8. **Recall lane catalog** — `memory/recall_posture.py`
   - POSTURE_CATALOG may have hardcoded lane weights for specific subdomains
   - **Risk:** New dynamic subdomains don't appear in lane configurations

9. **DeepSleep consolidation** — `deepsleep_loop/consolidate_memory/run.py`
   - Lines 18, 20 may reference KNOWLEDGE_DOMAINS
   - **Risk:** Consolidation fails or misroutes memories with dynamic subdomains

10. **Omega runtime serving** — Separate system at `F:\omega-portable-lite\`
    - SetFit model loading/serving configuration
    - Model lifecycle management
    - **Risk:** Runtime changes not coordinated with brain code changes cause boot failure

11. **MRIL tools** — `auto_loop/mril_tools/subdomain_helpers.py`
    - Lines 8, 24, 30 reference SUBDOMAIN_MAP
    - **Risk:** Auto-loop subdomain tools break with dynamic registry

12. **Answerability index** — `memory/answerability_index.py`
    - May aggregate scores by static subdomain set
    - **Risk:** New subdomains don't get answerability scores

13. **Memory consolidation** — `memory/consolidation.py`
    - May use subdomain grouping for consolidation logic
    - **Risk:** Consolidation misses or misgroups dynamic subdomains

14. **Guardian state persistence** — `guardian/state_persistence.py`
    - May snapshot subdomain structure
    - **Risk:** State snapshots contain static structure, break after dynamic transition

15. **Telemetry/audit** — Various `emit_data()` calls
    - May log subdomain names assuming static set
    - **Risk:** Telemetry contains unknown subdomain names, breaks dashboards

---

## RECOMMENDED BUILD ORDER

### Dependencies graph:
```
day_zero (lobes 2/8 blank) 
    ↓
subdomain_allocator (new module)
    ↓
dewey_map (dynamic lookup)
    ↓
write_path_routing (allocator integration)
    ↓
SetFit_retirement (runtime config + classifier)
    ↓
HUD_redesign (occupation/subcategory form)
    ↓
topic_resolution_bridge (seeded subdomain → topic)
    ↓
consumer_updates (validate_write, recall_lanes, enrichment)
    ↓
recall_index_updates (answerability, coord_index handling)
    ↓
test_updates (all affected tests)
    ↓
integration_verification (day-zero → onboarding → first LEARN)
```

### Phased sequence:

**Phase 1: Foundation (no functional change)**
1. Create `memory/subdomain_router.py` with Y allocator (unwired)
2. Add `guardian/state/dynamic_y_index.json` schema
3. Add `resolve_subdomain_dir()` helper in dewey_map.py (unwired)
4. Update `day_zero_reset.py` to reset dynamic_y_index (unwired)

**Phase 2: Taxonomy preparation**
5. Modify dewey_map.py to support dynamic lookup (add DYNAMIC_Y_LOBES)
6. Update `memory/engine/validate_write.py` to use `resolve_subdomain_dir()`
7. Update `memory/knowledge_router.py` to count dynamic Y
8. Update `memory/engine/hierarchical_classifier.py` lexicon for dynamic subdomains

**Phase 3: Write path integration**
9. Wire subdomain allocator into gate.ingest() for lobes 2 & 8
10. Add Y allocation step after classifier prediction
11. Update `memory/gates/base.py` to call allocator for dynamic lobes
12. Test allocator with manual writes (no HUD yet)

**Phase 4: SetFit retirement**
13. Update `config/runtime.yaml`: remove setfit_knowledge/setfit_coding slots
14. Update `memory/engine/hierarchical_classifier.py` to skip subdomain classifier for X=2/8
15. Add name derivation fallback for subdomain prediction
16. Coordinate runtime side changes (separate system)

**Phase 5: Day-zero transition**
17. Modify `scripts/day_zero_reset.py` to create NO subdomain dirs for lobes 2/8
18. Add `reset_dynamic_y_index()` to reset sequence
19. Decide label_map.json handling (preserve with deprecation notes)
20. Test day-zero → lobes 2/8 are blank

**Phase 6: HUD onboarding**
21. Add occupation/subcategory vocabulary to `hud/settings.py`
22. Redesign `hud/omega_interface.html` profile form
23. Update `hud/server.py` POST route for new payload
24. Wire profile SAVE to subdomain allocator
25. Add starter topic planting logic

**Phase 7: Topic resolution bridge**
26. Add "seeded subdomain" resolver to topic cascade
27. Update trajectory.target_domain to use occupation/subcategory
28. Ensure `canonical_domain_key()` works with dynamic names
29. Test: onboarding → topic resolution returns seeded subdomain

**Phase 8: Consumer updates**
30. Update `memory/recall_lanes.py` for dynamic subdomain enumeration
31. Update enrichment modules if they assume static structure
32. Update answerability index for dynamic aggregation
33. Update `memory/coding_project.py` for dynamic CODING_DOMAINS

**Phase 9: Test migration**
34. Update all tests pinning static Y values
35. Add allocator setup/teardown to test fixtures
36. Update classifier mocks for lobes 2/8
37. Replace directory structure assertions with dynamic checks
38. Add integration test: day-zero → onboarding → first LEARN

**Phase 10: Verification**
39. Run full test suite
40. Manual verification: day-zero → onboarding → first LEARN resolves to seeded topic (not epistemic_emptiness)
41. Audit coord_index.json for mixed static/dynamic Y
42. Verify recall works for both historical (static) and new (dynamic) records
43. Update documentation (AGENTS.md, README files)

**Phase 11: Runtime coordination (separate)**
44. Deploy runtime config changes
45. Unload retired SetFit models
46. Update label_map.json
47. Verify runtime health

### Critical path (must be sequential):
- Allocator → dewey_map → write_path → SetFit_retirement → day_zero → HUD → topic_bridge → tests
- Other consumer updates can happen in parallel after write_path is stable

### Parallelizable work:
- Consumer updates (enrichment, recall, answerability) can proceed in parallel after Phase 4
- Test updates can proceed in parallel after Phase 7
- Runtime changes (Phase 11) are independent but must coordinate before Phase 10 verification

---

## Summary of Biggest Risks

1. **Historical record isolation:** Old memories with static Y values may not resolve to meaningful directory names after dynamic transition, potentially causing data loss or recall failures. The coord_index.json will contain a mix of static and dynamic Y values with no migration path.

2. **Classifier quality regression:** Retiring the subdomain SetFit models removes semantic classification for lobes 2 and 8. Name derivation is deterministic but may be significantly less accurate than the learned models, leading to misrouted writes and degraded recall quality.

3. **Boot failure on empty lobes:** After day-zero, lobes 2 and 8 will have NO subdomain directories. If the classifier or allocator fails, or if the operator doesn't complete onboarding, the system may be in a non-functional state where every episode resolves to NO_TOPIC_AVAILABLE (epistemic_emptiness).

4. **Runtime coordination complexity:** Changes to brain code (classifier retirement, dynamic lookup) must sync with runtime configuration (model unloading, label_map updates). If these systems diverge, the brain may call classifier slots that no longer exist or receive predictions from stale models.

5. **Test migration surface area:** Nearly every memory test pins static Y values or assumes static directory structure. Migrating these tests to use dynamic allocation is a large undertaking with high risk of introducing test flakiness or missing coverage gaps.
