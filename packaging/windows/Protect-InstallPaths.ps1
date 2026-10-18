# Embedded as -EncodedCommand by Build-Windows.ps1; never run from a temp script.
# This preflight does not delete, move, or recursively change existing data.
$ErrorActionPreference = 'Stop'
$PSModuleAutoLoadingPreference = 'None'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not [Environment]::Is64BitProcess -or -not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'An elevated 64-bit installer is required.'
}

$admin = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
$appPath = [IO.Path]::Combine([Environment]::GetFolderPath('ProgramFiles'), 'Elipro UPS')
$dataParent = [IO.Path]::Combine([Environment]::GetFolderPath('CommonApplicationData'), 'Elipro UPS')
$dataPath = [IO.Path]::Combine($dataParent, 'data')
$inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$allow = [Security.AccessControl.AccessControlType]::Allow
$full = [int][Security.AccessControl.FileSystemRights]::FullControl
$read = [int][Security.AccessControl.FileSystemRights]::ReadAndExecute -bor [int][Security.AccessControl.FileSystemRights]::Synchronize

function Assert-NoReparseParents([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        try {
            $attributes = [IO.File]::GetAttributes($cursor)
            if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Elipro installation paths must not contain links or junctions.'
            }
        }
        catch [IO.FileNotFoundException] { }
        catch [IO.DirectoryNotFoundException] { }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
}

function New-SafeAcl([bool]$Readable) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($admin)
    foreach ($sid in @($admin, $system)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, $full, $inherit, 'None', $allow)
        $acl.AddAccessRule($rule)
    }
    if ($Readable) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($users, $read, $inherit, 'None', $allow)
        $acl.AddAccessRule($rule)
    }
    return $acl
}

function Assert-SafeAcl([IO.FileSystemInfo]$Item, [bool]$Readable, [bool]$Root) {
    $acl = $Item.GetAccessControl()
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin @($admin.Value, $system.Value)) { throw 'An existing Elipro object has an untrusted owner.' }
    if ($Root -and -not $acl.AreAccessRulesProtected) { throw 'An existing Elipro directory inherits permissions.' }
    $seen = @{}
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        $sid = $rule.IdentityReference.Value
        $rights = [int]$rule.FileSystemRights
        if ($rule.AccessControlType -ne $allow) { throw 'An existing Elipro permission rule is not supported.' }
        if ($sid -in @($admin.Value, $system.Value)) {
            if ($rights -ne $full) { throw 'Administrators and SYSTEM require full control of Elipro.' }
        }
        elseif ($Readable -and $sid -eq $users.Value) {
            if (($rights -band (-bnot $read)) -ne 0 -or ($rights -band $read) -ne $read) {
                throw 'Users must have read-only access to the installed application.'
            }
        }
        else { throw 'An existing Elipro permission rule grants access to an untrusted identity.' }
        if ($Root -and ($rule.InheritanceFlags -ne $inherit -or [int]$rule.PropagationFlags -ne 0)) {
            throw 'Elipro directory permissions must propagate to new children.'
        }
        $seen[$sid] = $true
    }
    if (-not $seen[$admin.Value] -or -not $seen[$system.Value] -or ($Readable -and -not $seen[$users.Value])) {
        throw 'Required Elipro permissions are missing.'
    }
}

function Protect-Directory([string]$Path, [bool]$Readable) {
    Assert-NoReparseParents $Path
    $directory = [IO.DirectoryInfo]::new($Path)
    # DirectoryInfo.Create(DirectorySecurity) applies the ACL at creation. If an
    # attacker has already created the directory, no ACL is changed: validation
    # below rejects it. Existing insecure folders require manual admin review.
    $directory.Create((New-SafeAcl $Readable))
    Assert-NoReparseParents $Path
    Assert-SafeAcl $directory $Readable $true
    $pending = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $pending.Push($directory)
    $count = 0
    while ($pending.Count) {
        foreach ($item in $pending.Pop().GetFileSystemInfos()) {
            $count++
            if ($count -gt 100000) { throw 'The existing Elipro directory is too large for safe validation.' }
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'An existing Elipro directory contains a link or junction.'
            }
            Assert-SafeAcl $item $Readable $false
            if ($item -is [IO.DirectoryInfo]) { $pending.Push($item) }
        }
    }
}

# Validate every ancestor before any directory is created. No user-controlled
# destination or recursive ACL rewrite is accepted by this preflight.
foreach ($path in @($appPath, $dataParent, $dataPath)) { Assert-NoReparseParents $path }
Protect-Directory $appPath $true
Protect-Directory $dataParent $false
Protect-Directory $dataPath $false
'Elipro installation paths and protected permissions are valid.'
