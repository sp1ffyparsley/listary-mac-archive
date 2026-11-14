#import <AppKit/AppKit.h>

static NSString *ArgumentValue(NSArray<NSString *> *arguments, NSString *name) {
    NSUInteger index = [arguments indexOfObject:name];
    if (index == NSNotFound || index + 1 >= arguments.count) return nil;
    return arguments[index + 1];
}

@interface WindowFixtureDelegate : NSObject <NSApplicationDelegate, NSTableViewDataSource, NSTableViewDelegate>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) NSTextField *queryField;
@property(nonatomic, copy) NSString *statusPath;
@end

@implementation WindowFixtureDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    NSArray<NSString *> *arguments = NSProcessInfo.processInfo.arguments;
    self.statusPath = ArgumentValue(arguments, @"--status");

    NSRect frame = NSMakeRect(0, 0, 680, 420);
    self.window = [[NSWindow alloc]
        initWithContentRect:frame
                  styleMask:(NSWindowStyleMaskTitled |
                             NSWindowStyleMaskClosable |
                             NSWindowStyleMaskResizable)
                    backing:NSBackingStoreBuffered
                      defer:NO];
    self.window.title = @"Listary WindowFixture";
    self.window.releasedWhenClosed = NO;
    self.window.accessibilityIdentifier = @"ListaryWindowFixtureWindow";

    NSView *content = self.window.contentView;
    self.queryField = [[NSTextField alloc] initWithFrame:NSMakeRect(24, 360, 632, 28)];
    self.queryField.placeholderString = @"Fixture query";
    self.queryField.stringValue = @"fixture";
    self.queryField.accessibilityIdentifier = @"ListaryWindowFixtureQuery";
    [content addSubview:self.queryField];

    NSScrollView *scroll = [[NSScrollView alloc] initWithFrame:NSMakeRect(24, 72, 632, 268)];
    scroll.hasVerticalScroller = YES;
    NSTableView *table = [[NSTableView alloc] initWithFrame:scroll.bounds];
    table.accessibilityIdentifier = @"ListaryWindowFixtureResults";
    NSTableColumn *column = [[NSTableColumn alloc] initWithIdentifier:@"Result"];
    column.title = @"Result";
    column.width = 610;
    [table addTableColumn:column];
    table.headerView = nil;
    table.dataSource = self;
    table.delegate = self;
    scroll.documentView = table;
    [content addSubview:scroll];

    NSTextField *hint = [[NSTextField alloc] initWithFrame:NSMakeRect(24, 24, 632, 28)];
    hint.stringValue = @"Acceptance-only AppKit fixture — no production files or settings.";
    hint.editable = NO;
    hint.selectable = NO;
    hint.bezeled = NO;
    hint.drawsBackground = NO;
    hint.accessibilityIdentifier = @"ListaryWindowFixtureHint";
    [content addSubview:hint];

    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    [self.window makeFirstResponder:self.queryField];
    [self writeStatus:@"ready"];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
    (void)sender;
    return YES;
}

- (NSInteger)numberOfRowsInTableView:(NSTableView *)tableView {
    (void)tableView;
    return 3;
}

- (NSView *)tableView:(NSTableView *)tableView
    viewForTableColumn:(NSTableColumn *)tableColumn
                   row:(NSInteger)row {
    (void)tableColumn;
    NSString *identifier = @"FixtureCell";
    NSTextField *field = [tableView makeViewWithIdentifier:identifier owner:self];
    if (field == nil) {
        field = [[NSTextField alloc] initWithFrame:NSMakeRect(0, 0, 610, 24)];
        field.identifier = identifier;
        field.editable = NO;
        field.selectable = NO;
        field.bezeled = NO;
        field.drawsBackground = NO;
    }
    field.stringValue = [NSString stringWithFormat:@"Fixture result %ld", (long)(row + 1)];
    return field;
}

- (void)writeStatus:(NSString *)state {
    if (self.statusPath.length == 0) return;
    id responder = self.window.firstResponder;
    BOOL queryFocused = responder == self.queryField ||
        ([responder isKindOfClass:[NSTextView class]] &&
         [(NSTextView *)responder delegate] == self.queryField);
    NSDictionary *payload = @{
        @"schema": @"listary_window_fixture_v1",
        @"state": state,
        @"pid": @(NSProcessInfo.processInfo.processIdentifier),
        @"window_title": self.window.title ?: @"",
        @"query_accessibility_identifier": @"ListaryWindowFixtureQuery",
        @"query_focused": @(queryFocused)
    };
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:NSJSONWritingPrettyPrinted error:&error];
    if (data == nil || error != nil) return;
    [data writeToFile:self.statusPath options:NSDataWritingAtomic error:nil];
}

@end

int main(int argc, const char *argv[]) {
    (void)argc;
    (void)argv;
    @autoreleasepool {
        NSApplication *application = [NSApplication sharedApplication];
        WindowFixtureDelegate *delegate = [[WindowFixtureDelegate alloc] init];
        application.delegate = delegate;
        [application run];
    }
    return 0;
}
