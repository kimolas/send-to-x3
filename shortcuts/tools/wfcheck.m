// wfcheck: load a shortcut file with Apple's own WorkflowKit and report how the
// Shortcuts app reads it, without importing it into anyone's Shortcuts library.
//
// Development aid for shortcuts/build_shortcut.py (used by tests/test_shortcuts_macos.py).
// WorkflowKit is a private macOS framework, so this only builds and runs on macOS;
// Apple can change it in any update. Actions implemented in ActionKit cannot be loaded
// into an ordinary process (ActionKit asserts it runs in a platform binary), so those
// show up as WFMissingAction with whatever parameter definitions WorkflowKit has.
//
// Build:  clang -fobjc-arc -framework Foundation wfcheck.m -o wfcheck
//
// Commands (all print one JSON object on stdout):
//   wfcheck decode <file>
//       actions (class, decoded parameter-state classes, unknown keys, conditions as the
//       app's condition table), import questions as WorkflowKit parses and re-serializes
//       them, the no-input behavior class, and what WorkflowKit's import migrations change.
//   wfcheck eval <file> <action-index> [NAME=VALUE | NAME= | NAME:= ...]
//       Runs one WorkflowKit-native action (an If condition, or Get Details of Safari Web
//       Page) with the named variables set: NAME=VALUE is one text item, NAME= is no
//       items, NAME:= is one empty text item. Environment WF_INPUT_KIND
//       (text|url|page|rich) and WF_INPUT (a string; for page the page URL, with
//       WF_PAGE_TITLE) provide the Shortcut Input.
//   wfcheck params <file> <action-index> [NAME=VALUE ...]
//       Resolves the action's parameters the way a run does (variables substituted),
//       including its input parameter.
//   wfcheck regex <pattern> <template> <case-sensitive 0|1>   (JSON array of strings on stdin)
//       Replaces every match in each input with NSRegularExpression, the ICU engine behind
//       Replace Text with Regular Expression on (Replace Text itself is in ActionKit), and
//       prints {"outputs": [...]}: pins the regex semantics shortcuts/tools/simulate.py uses.

#import <Foundation/Foundation.h>
#import <objc/message.h>
#import <objc/runtime.h>
#include <dlfcn.h>

#define MSG(ret, ...) ((ret(*)(__VA_ARGS__))objc_msgSend)

static id call0(id o, const char *s) {
  SEL sel = sel_registerName(s);
  if (!o || ![o respondsToSelector:sel]) return nil;
  return MSG(id, id, SEL)(o, sel);
}
static id call1(id o, const char *s, id a) {
  SEL sel = sel_registerName(s);
  if (!o || ![o respondsToSelector:sel]) return nil;
  return MSG(id, id, SEL, id)(o, sel, a);
}

static void die(NSString *msg) {
  fprintf(stderr, "wfcheck: %s\n", msg.UTF8String);
  exit(2);
}

static void wait_until(BOOL *flag, double seconds) {
  NSDate *limit = [NSDate dateWithTimeIntervalSinceNow:seconds];
  while (!*flag && [limit timeIntervalSinceNow] > 0)
    [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.02]];
}

static id jsonable(id v);

static NSString *item_value(id item) {
  static const char *getters[] = {"emailAddress", "URL", "string"};
  for (size_t i = 0; i < sizeof getters / sizeof *getters; i++) {
    id x = call0(item, getters[i]);
    if (call0(x, "address")) x = call0(x, "address");  // WFEmailAddress
    if (x) return [x isKindOfClass:[NSURL class]] ? [x absoluteString] : [x description];
  }
  id rep = call0(item, "internalRepresentation");
  id obj = call0(rep, "object");
  if ([obj isKindOfClass:[NSURL class]]) return [obj absoluteString];
  if ([obj isKindOfClass:[NSString class]]) return obj;
  if ([obj isKindOfClass:[NSAttributedString class]]) return [obj string];
  if (obj) return [obj description];
  return [item description];
}

static NSArray *collection_items(id coll) {
  NSMutableArray *out = [NSMutableArray array];
  for (id it in call0(coll, "items") ?: @[])
    [out addObject:@{@"class" : NSStringFromClass([it class]), @"value" : item_value(it) ?: @""}];
  return out;
}

static id jsonable(id v) {
  if (!v) return [NSNull null];
  if ([v isKindOfClass:[NSDictionary class]]) {
    NSMutableDictionary *d = [NSMutableDictionary dictionary];
    for (id k in v) d[[k description]] = jsonable(v[k]);
    return d;
  }
  if ([v isKindOfClass:[NSArray class]] || [v isKindOfClass:[NSSet class]] || [v isKindOfClass:[NSOrderedSet class]]) {
    NSMutableArray *a = [NSMutableArray array];
    for (id x in v) [a addObject:jsonable(x)];
    return a;
  }
  if ([v isKindOfClass:[NSString class]] || [v isKindOfClass:[NSNumber class]]) return v;
  if ([v isKindOfClass:[NSURL class]]) return [v absoluteString];
  if ([v isKindOfClass:[NSData class]]) return [NSString stringWithFormat:@"<%lu bytes>", (unsigned long)[v length]];
  if ([v isKindOfClass:objc_getClass("WFContentCollection")]) return collection_items(v);
  if ([v isKindOfClass:objc_getClass("WFContentItem")]) return item_value(v);
  if ([v isKindOfClass:objc_getClass("WFProcessedContacts")]) {
    NSMutableArray *a = [NSMutableArray array];
    for (id it in call0(call0(v, "contentCollection"), "items") ?: @[]) [a addObject:item_value(it)];
    return @{@"contacts" : a};
  }
  if ([v isKindOfClass:[NSError class]]) return [v localizedDescription];
  return [NSString stringWithFormat:@"<%@>", NSStringFromClass([v class])];
}

static void print_json(id obj) {
  NSError *err = nil;
  NSData *json = [NSJSONSerialization dataWithJSONObject:jsonable(obj)
                                                 options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys
                                                   error:&err];
  if (!json) die(err.description);
  fwrite(json.bytes, 1, json.length, stdout);
  fputc('\n', stdout);
}

static NSData *read_file(const char *path) {
  NSData *data = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:path]];
  if (!data) die([NSString stringWithFormat:@"cannot read %s", path]);
  return data;
}

static id load_workflow(NSData *data) {
  NSError *err = nil;
  id file = MSG(id, id, SEL, id, id, NSError **)([objc_getClass("WFWorkflowFile") alloc],
                                                 sel_registerName("initWithFileData:name:error:"), data, @"wfcheck", &err);
  if (!file) die([NSString stringWithFormat:@"not a shortcut file: %@", err]);
  id record = MSG(id, id, SEL, NSError **)(file, sel_registerName("recordRepresentationWithError:"), &err);
  if (!record) die([NSString stringWithFormat:@"no workflow record: %@", err]);
  id wf = MSG(id, id, SEL, id, id, id, NSError **)([objc_getClass("WFWorkflow") alloc],
                                                   sel_registerName("initWithRecord:reference:storageProvider:error:"),
                                                   record, nil, nil, &err);
  if (!wf) die([NSString stringWithFormat:@"WorkflowKit rejected the workflow: %@", err]);
  return wf;
}

// ------------------------------------------------------------------------------- decode

static NSDictionary *decode_action(id a) {
  NSMutableDictionary *ad = [NSMutableDictionary dictionary];
  ad[@"class"] = NSStringFromClass([a class]);
  ad[@"identifier"] = call0(a, "identifier") ?: @"";
  NSMutableArray *keys = [NSMutableArray array];
  for (id p in call0(a, "parameters")) [keys addObject:call0(p, "key") ?: @"?"];
  ad[@"definition_keys"] = keys;
  NSDictionary *sp = call0(a, "serializedParameters");
  NSMutableDictionary *states = [NSMutableDictionary dictionary];
  NSMutableArray *unknown = [NSMutableArray array];
  for (id k in sp) {
    id st = call1(a, "parameterStateForKey:", k);
    if (st) states[k] = NSStringFromClass([st class]);
    else if (![@[ @"UUID", @"GroupingIdentifier", @"WFControlFlowMode" ] containsObject:k]) [unknown addObject:k];
  }
  ad[@"states"] = states;
  ad[@"unknown_keys"] = [unknown sortedArrayUsingSelector:@selector(compare:)];
  ad[@"serialized_parameters"] = jsonable(sp);
  // Condition table the app builds (from WFConditions, or from the single-condition keys).
  id cond = MSG(id, id, SEL, id, BOOL)(a, sel_registerName("parameterStateForKey:fallingBackToDefaultValue:"),
                                       @"WFConditions", NO);
  if (cond && [a isKindOfClass:objc_getClass("WFConditionalAction")])
    ad[@"conditions"] = jsonable(call0(cond, "serializedRepresentation"));
  return ad;
}

static NSDictionary *migration_report(NSData *data) {
  NSError *err = nil;
  NSDictionary *dict = [NSPropertyListSerialization propertyListWithData:data
                                                                 options:NSPropertyListMutableContainersAndLeaves
                                                                  format:NULL
                                                                   error:&err];
  id f = MSG(id, id, SEL, id, id, BOOL)([objc_getClass("WFWorkflowFile") alloc],
                                        sel_registerName("initWithDictionary:name:performMigration:"), dict, @"wfcheck", YES);
  NSData *out = MSG(id, id, SEL, NSError **)(f, sel_registerName("fileDataWithError:"), &err);
  if (!out) return @{@"error" : err.description ?: @"?"};
  NSDictionary *migrated = [NSPropertyListSerialization propertyListWithData:out options:0 format:NULL error:&err];
  NSArray *before = dict[@"WFWorkflowActions"], *after = migrated[@"WFWorkflowActions"];
  NSMutableArray *changed = [NSMutableArray array];
  for (NSUInteger i = 0; i < MAX(before.count, after.count); i++)
    if (i >= before.count || i >= after.count || ![before[i] isEqual:after[i]]) [changed addObject:@(i)];
  NSMutableArray *topChanged = [NSMutableArray array];
  NSMutableSet *allKeys = [NSMutableSet setWithArray:dict.allKeys];
  [allKeys addObjectsFromArray:migrated.allKeys];
  for (NSString *k in allKeys)
    if (![k isEqual:@"WFWorkflowActions"] && ![dict[k] isEqual:migrated[k]]) [topChanged addObject:k];
  return @{
    @"changed_actions" : changed,
    @"changed_keys" : [topChanged sortedArrayUsingSelector:@selector(compare:)],
    @"client_version" : migrated[@"WFWorkflowClientVersion"] ?: [NSNull null],
  };
}

static int cmd_decode(const char *path) {
  NSData *data = read_file(path);
  id wf = load_workflow(data);
  NSArray *actions = call0(wf, "actions");
  NSMutableArray *acts = [NSMutableArray array];
  for (id a in actions) [acts addObject:decode_action(a)];
  NSMutableArray *qs = [NSMutableArray array];
  for (id q in call0(wf, "importQuestions")) {
    id act = call0(q, "action");
    [qs addObject:@{
      @"text" : call0(q, "question") ?: [NSNull null],
      @"action_index" : act ? @([actions indexOfObjectIdenticalTo:act]) : [NSNull null],
      @"parameter" : call0(call0(q, "parameter"), "key") ?: [NSNull null],
      @"default_state" : call0(q, "defaultState") ? NSStringFromClass([call0(q, "defaultState") class]) : [NSNull null],
      @"reserialized" : jsonable(call1(q, "serializedRepresentationWithWorkflowActions:", actions)),
    }];
  }
  id nib = call0(wf, "noInputBehavior");
  print_json(@{
    @"actions" : acts,
    @"import_questions" : qs,
    @"no_input_behavior" : nib ? NSStringFromClass([nib class]) : [NSNull null],
    @"migration" : migration_report(data),
  });
  return 0;
}

// ------------------------------------------------------------------- eval and params

static id collection_with(NSArray *objects) {
  NSMutableArray *items = [NSMutableArray array];
  for (id o in objects) {
    id item = MSG(id, id, SEL, id)(objc_getClass("WFContentItem"), sel_registerName("itemWithObject:"), o);
    if (item) [items addObject:item];
  }
  return MSG(id, id, SEL, id)(objc_getClass("WFContentCollection"), sel_registerName("collectionWithItems:"), items);
}

static id shortcut_input(void) {
  const char *kind = getenv("WF_INPUT_KIND");
  if (!kind) return nil;
  NSString *value = [NSString stringWithUTF8String:getenv("WF_INPUT") ?: ""];
  id obj = nil;
  if (!strcmp(kind, "text")) obj = value;
  else if (!strcmp(kind, "url")) obj = [NSURL URLWithString:value];
  else if (!strcmp(kind, "rich")) obj = [[NSAttributedString alloc] initWithString:value];
  else if (!strcmp(kind, "page")) {
    NSString *title = [NSString stringWithUTF8String:getenv("WF_PAGE_TITLE") ?: "Page"];
    NSData *html = [@"<html><body><p>Page</p></body></html>" dataUsingEncoding:NSUTF8StringEncoding];
    obj = MSG(id, id, SEL, id, id, id)([objc_getClass("WFSafariWebPage") alloc],
                                       sel_registerName("initWithURL:pageTitle:documentHTML:"),
                                       [NSURL URLWithString:value], title, html);
  } else die([NSString stringWithFormat:@"unknown WF_INPUT_KIND %s", kind]);
  return collection_with(obj ? @[ obj ] : @[]);
}

// A WFWorkflowController serves as the variable source, holding the variables (and the
// Shortcut Input) given on the command line.
static id make_controller(id wf, int argc, char **argv, int first) {
  id ctl = [[objc_getClass("WFWorkflowController") alloc] init];
  MSG(void, id, SEL, id)(ctl, sel_registerName("setWorkflow:"), wf);
  id input = shortcut_input();
  if (input) {
    MSG(void, id, SEL, id)(ctl, sel_registerName("setInput:"), input);
    // The input normally reaches the controller when a run starts; this process never
    // starts one, so answer workflowInput from the input set above (process-local).
    Method m = class_getInstanceMethod(objc_getClass("WFWorkflowController"), sel_registerName("workflowInput"));
    method_setImplementation(m, imp_implementationWithBlock(^id(id self) { return call0(self, "input"); }));
  }
  for (int i = first; i < argc; i++) {
    NSString *arg = [NSString stringWithUTF8String:argv[i]];
    NSRange eq = [arg rangeOfString:@"="];
    if (eq.location == NSNotFound) die([NSString stringWithFormat:@"expected NAME=VALUE, got %@", arg]);
    NSString *name = [arg substringToIndex:eq.location], *value = [arg substringFromIndex:eq.location + 1];
    // NAME=VALUE sets one text item; NAME= sets nothing (no items); NAME:= sets one
    // empty text item (what an empty Text action produces).
    BOOL emptyItem = [name hasSuffix:@":"];
    if (emptyItem) name = [name substringToIndex:name.length - 1];
    MSG(BOOL, id, SEL, id, id)(ctl, sel_registerName("setContent:forVariableWithName:"),
                               collection_with(value.length || emptyItem ? @[ value ] : @[]), name);
  }
  return ctl;
}

static id action_at(id wf, const char *index) {
  NSArray *actions = call0(wf, "actions");
  long i = atol(index);
  if (i < 0 || (NSUInteger)i >= actions.count) die(@"action index out of range");
  return actions[(NSUInteger)i];
}

static int cmd_eval(int argc, char **argv) {
  id wf = load_workflow(read_file(argv[2]));
  id action = action_at(wf, argv[3]);
  id ctl = make_controller(wf, argc, argv, 4);
  MSG(void, id, SEL, id)(action, sel_registerName("setVariableSource:"), ctl);
  BOOL isIf = [action isKindOfClass:objc_getClass("WFConditionalAction")];
  id input = call0(ctl, "input") ?: collection_with(@[]);
  dispatch_queue_t q = dispatch_queue_create("wfcheck", DISPATCH_QUEUE_SERIAL);
  __block BOOL done = NO;
  __block NSDictionary *result = nil;
  MSG(void, id, SEL, id, id, id, id, id, id)(
      action, sel_registerName("runWithInput:userInterface:runningDelegate:variableSource:workQueue:completionHandler:"),
      input, nil, nil, ctl, q, ^(id output) {
        if (isIf) {
          BOOL t = MSG(BOOL, id, SEL, id)(action, sel_registerName("truthWithVariableSource:"), ctl);
          result = @{@"kind" : @"condition", @"truth" : @(t)};
        } else {
          id out = call0(action, "output") ?: output;
          result = @{@"kind" : @"output", @"items" : collection_items(out)};
        }
        done = YES;
      });
  wait_until(&done, 10);
  print_json(result ?: @{@"kind" : @"timeout"});
  return result ? 0 : 1;
}

static int cmd_params(int argc, char **argv) {
  id wf = load_workflow(read_file(argv[2]));
  id action = action_at(wf, argv[3]);
  id ctl = make_controller(wf, argc, argv, 4);
  MSG(void, id, SEL, id)(action, sel_registerName("setVariableSource:"), ctl);
  id input = call0(ctl, "input") ?: collection_with(@[]);
  dispatch_queue_t q = dispatch_queue_create("wfcheck", DISPATCH_QUEUE_SERIAL);
  NSMutableDictionary *result = [NSMutableDictionary dictionary];

  __block BOOL done = NO;
  MSG(void, id, SEL, id, id, id)(action, sel_registerName("processParametersWithoutAskingForValuesWithInput:workQueue:completionHandler:"),
                                 input, q, ^(id processed, id error) {
                                   result[@"processed"] = jsonable(call0(action, "processedParameters") ?: processed);
                                   done = YES;
                                 });
  wait_until(&done, 10);

  // The input parameter (e.g. Get Contents of URL's URL, Send Email's message) is not
  // part of processedParameters; resolve it separately.
  NSString *inputKey = call0(action, "inputParameterKey");
  id param = inputKey ? call1(action, "parameterForKey:", inputKey) : nil;
  id state = inputKey ? call1(action, "parameterStateForKey:", inputKey) : nil;
  if (param && state) {
    id tracker = [[objc_getClass("WFContentAttributionTracker") alloc] init];
    id ctx = MSG(id, id, SEL, id, id, BOOL, long long, id, long long)(
        [objc_getClass("WFParameterStateProcessingContext") alloc],
        sel_registerName("initWithVariableSource:parameter:isInputParameter:environment:contentAttributionTracker:widgetSizeClass:"),
        ctl, param, YES, 0, tracker, 0);
    __block BOOL idone = NO;
    MSG(void, id, SEL, id, id, id)(state, sel_registerName("processWithContext:userInputRequiredHandler:valueHandler:"), ctx,
                                   ^(id a, id b) { idone = YES; },
                                   ^(id value, id error) {
                                     result[@"input_parameter"] = @{@"key" : inputKey, @"value" : jsonable(value)};
                                     idone = YES;
                                   });
    wait_until(&idone, 10);
  }
  print_json(result);
  return done ? 0 : 1;
}

// -------------------------------------------------------------------------------- regex

static int cmd_regex(char **argv) {
  NSError *err = nil;
  NSRegularExpressionOptions opts = atoi(argv[4]) ? 0 : NSRegularExpressionCaseInsensitive;
  NSRegularExpression *re = [NSRegularExpression regularExpressionWithPattern:[NSString stringWithUTF8String:argv[2]]
                                                                      options:opts
                                                                        error:&err];
  if (!re) die(err.description);
  NSData *stdinData = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
  NSArray *inputs = [NSJSONSerialization JSONObjectWithData:stdinData options:0 error:&err];
  if (![inputs isKindOfClass:[NSArray class]]) die(@"stdin must be a JSON array of strings");
  NSString *template = [NSString stringWithUTF8String:argv[3]];
  NSMutableArray *outputs = [NSMutableArray array];
  for (NSString *s in inputs)
    [outputs addObject:[re stringByReplacingMatchesInString:s
                                                    options:0
                                                      range:NSMakeRange(0, s.length)
                                               withTemplate:template]];
  print_json(@{@"outputs" : outputs});
  return 0;
}

int main(int argc, char **argv) {
  @autoreleasepool {
    if (!dlopen("/System/Library/PrivateFrameworks/WorkflowKit.framework/WorkflowKit", RTLD_NOW))
      die(@"cannot load WorkflowKit (macOS only)");
    if (argc >= 3 && !strcmp(argv[1], "decode")) return cmd_decode(argv[2]);
    if (argc >= 4 && !strcmp(argv[1], "eval")) return cmd_eval(argc, argv);
    if (argc >= 4 && !strcmp(argv[1], "params")) return cmd_params(argc, argv);
    if (argc >= 5 && !strcmp(argv[1], "regex")) return cmd_regex(argv);
    fprintf(stderr, "usage: wfcheck decode <file> | eval <file> <index> [NAME=VALUE...] | params <file> <index> [NAME=VALUE...] | regex <pattern> <template> <0|1>\n");
    return 2;
  }
}
