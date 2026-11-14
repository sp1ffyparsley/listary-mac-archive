#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>

static NSString *StatusPath = nil;
static NSString *FixtureMode = nil;
static NSSavePanel *FixturePanel = nil;

static void WriteStatus(BOOL visible, BOOL closed) {
    NSString *directory = FixturePanel.directoryURL.path ?: @"";
    NSDictionary *payload = @{
        @"schema": @"listary_file_dialog_fixture_v1",
        @"mode": FixtureMode ?: @"unknown",
        @"pid": @([[NSProcessInfo processInfo] processIdentifier]),
        @"window_number": @(FixturePanel.windowNumber),
        @"title": FixturePanel.title ?: @"",
        @"current_directory": directory,
        @"visible": @(visible),
        @"closed": @(closed),
    };
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:NSJSONWritingPrettyPrinted error:nil];
    if (data != nil && StatusPath != nil) {
        [data writeToFile:StatusPath options:NSDataWritingAtomic error:nil];
    }
}

static NSString *ArgumentValue(NSArray<NSString *> *arguments, NSString *name) {
    NSUInteger index = [arguments indexOfObject:name];
    if (index == NSNotFound || index + 1 >= arguments.count) return nil;
    return arguments[index + 1];
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSArray<NSString *> *arguments = [[NSProcessInfo processInfo] arguments];
        FixtureMode = ArgumentValue(arguments, @"--mode") ?: @"open";
        NSString *initial = ArgumentValue(arguments, @"--initial") ?: NSHomeDirectory();
        StatusPath = ArgumentValue(arguments, @"--status");
        if (StatusPath.length == 0) {
            fprintf(stderr, "--status is required\n");
            return 2;
        }

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];

        if ([FixtureMode isEqualToString:@"save"]) {
            FixturePanel = [NSSavePanel savePanel];
            FixturePanel.title = @"Listary FileDialogFixture — Save";
            FixturePanel.nameFieldStringValue = @"ListaryAcceptance.txt";
        } else {
            NSOpenPanel *openPanel = [NSOpenPanel openPanel];
            openPanel.canChooseDirectories = YES;
            openPanel.canChooseFiles = NO;
            openPanel.allowsMultipleSelection = NO;
            openPanel.title = @"Listary FileDialogFixture — Open";
            FixturePanel = openPanel;
            FixtureMode = @"open";
        }

        FixturePanel.directoryURL = [NSURL fileURLWithPath:initial isDirectory:YES];
        [NSTimer scheduledTimerWithTimeInterval:0.10 repeats:YES block:^(__unused NSTimer *timer) {
            WriteStatus(FixturePanel.isVisible, NO);
        }];

        [NSApp activateIgnoringOtherApps:YES];
        [FixturePanel beginWithCompletionHandler:^(__unused NSModalResponse response) {
            WriteStatus(NO, YES);
            [NSApp terminate:nil];
        }];
        WriteStatus(YES, NO);
        [NSApp run];
    }
    return 0;
}
