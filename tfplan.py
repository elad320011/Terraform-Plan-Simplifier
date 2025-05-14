import os
import sys
import json
from glob import glob
from configurations.resource_types_map import resource_types

PROD_ENV = os.getenv("PROD_ENV").split(',') if os.getenv("PROD_ENV") else []
UAT_ENV = os.getenv("UAT_ENV").split(',') if os.getenv("UAT_ENV") else []
ROOT_DIRS = os.getenv("ROOT_DIRS").split(',') if os.getenv("ROOT_DIRS") else []

if not PROD_ENV or not UAT_ENV or not ROOT_DIRS:
    raise ValueError("Environment variables PROD_ENV, UAT_ENV, and ROOT_DIRS must be set and non-empty.")

artifact_folder = os.getenv("ARTIFACT_FOLDER", "default_artifact_folder")
tf_path = os.getenv("TF_PATH", "default_tf_path")
dirs_for_apply = []
fail_build = False

""" reason = "$(Build.Reason)"
if reason == "PullRequest":
  branch = "$(System.PullRequest.TargetBranch)"
else:
  branch = "$(Build.SourceBranch)" """

def run_cmd(command):
  global fail_build
  print(f"Running command {command}. Output:")
  p = os.popen(command)
  output = (p.readlines())
  exit_status = p.close()
  if (exit_status):
    fail_build = True
  #print(output)
  return output

def get_paths_for_tfplan():
  print("Check for changed files")
  files = run_cmd("git diff --name-only --relative --diff-filter AMR HEAD^ HEAD .")
  app_paths = []
  for file in files:
    file_path = file.rstrip().split("/")
    if "template" in file_path:
      continue
    print(file_path)
    if file_path[-1].split(".")[1] in ["tf", "tfvars"] and file_path[:-1] not in app_paths:
      app_paths.append(file_path[:-1])
  return app_paths

def process_file(file_path):
  # Read the lines from the file
  with open(file_path, 'r') as infile:
    lines = infile.readlines()

  # Process the lines
  with open(file_path, 'w') as outfile:
    for line in lines:
      if line.startswith('@@'):
        continue  # Skip lines that start with "@@"
      if line.startswith('# module') or line.startswith('    - module'):
        for resource_type in resource_types:
          if resource_type in line:
            if line.startswith("#"):
              line = '# ' + resource_types[resource_type] + ":" + line[1:]
              break
            if line.startswith("    - module"):
              line = '    - *' + resource_types[resource_type] + "*:" + line[5:]
              break
    
      outfile.write(line)  # Write the line to the file, modified or not

def tfj2md(file_name):
  run_cmd(f"cat {file_name}.json | /root/go/bin/terraform-j2md > {file_name}.md")
  process_file(f"{file_name}.md")

def filter_plan_json(file_name):
  """Create a filtered version of the Terraform plan JSON without tags and alerts"""
  try:
    with open(f"{file_name}.json", 'r') as f:
      plan_data = json.load(f)
      
    # Filter out resources with tags or alerts
    if 'resource_changes' in plan_data:
      filtered_changes = []
      coralogix_alerts = set()  # To track unique alert names
      tag_block_changes = 0
      tag_only_changes = 0
      
      for change in plan_data['resource_changes']:
        # Track Coralogix alerts by their unique name
        if change.get('type', '') == 'coralogix_alert' or 'coralogix_alert' in change.get('address', ''):
          # Extract alert name from address (last part after the dot or bracket)
          alert_name = change.get('address', '').split('.')[-1]
          if '[' in alert_name:
            alert_name = alert_name.split('[')[0]  # Handle indexed resources
          coralogix_alerts.add(alert_name)
          continue
        
        # Check if this change affects tags
        has_tag_changes = False
        other_changes = False
        
        if change.get('change', {}).get('actions', []) == ['update']:
          before = change.get('change', {}).get('before', {})
          after = change.get('change', {}).get('after', {})
          
          # Check if tags are changing
          if 'tags' in after or 'tags' in before:
            if before.get('tags', {}) != after.get('tags', {}):
              has_tag_changes = True
              tag_block_changes += 1
          
          # Check if there are other changes besides tags
          before_no_tags = {k: v for k, v in before.items() if k != 'tags'}
          after_no_tags = {k: v for k, v in after.items() if k != 'tags'}
          
          if before_no_tags != after_no_tags:
            other_changes = True
            
            # If we're updating complex objects, find only the changed attributes
            if change.get('change', {}).get('before_sensitive') == change.get('change', {}).get('after_sensitive'):
              # Only process for non-sensitive changes to avoid security issues
              simplified_changes = {}
              
              # Identify which top-level keys have changed
              for key in before_no_tags:
                if key in after_no_tags and before_no_tags[key] != after_no_tags[key]:
                  # If the value is a nested structure (dict or list), try to pinpoint specific changes
                  if isinstance(before_no_tags[key], (dict, list)) and isinstance(after_no_tags[key], (dict, list)):
                    # For nested objects, create a simplified representation
                    # Store only the key with a note that it changed
                    simplified_changes[key] = f"[Complex value changed]"
                  else:
                    # For simple values, store the before and after
                    simplified_changes[key] = {
                      "before": before_no_tags[key],
                      "after": after_no_tags[key]
                    }
              
              # Replace the full before/after with simplified changes when appropriate
              if simplified_changes and 'change' in change:
                change['change']['simplified_changes'] = simplified_changes
            
          # If only tags are changing, count it as a tag-only change
          if has_tag_changes and not other_changes:
            tag_only_changes += 1
            continue
            
        # Filter out tags
        if 'change' in change and 'after' in change['change']:
          # Remove tags if present
          if isinstance(change['change']['after'], dict) and 'tags' in change['change']['after']:
            del change['change']['after']['tags']
              
        filtered_changes.append(change)
          
      plan_data['resource_changes'] = filtered_changes
        
    # Write the filtered plan data to a clean JSON file
    clean_output_file = f"{file_name}_clean.json"
    with open(clean_output_file, 'w') as f:
      json.dump(plan_data, f, indent=2)
      
    # ANSI color codes for Terraform-like colors
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    GRAY = "\033[90m"
      
    # Convert the filtered JSON to text format that looks like terraform plan output
    clean_text_file = f"{file_name}_clean.txt"
    with open(clean_text_file, 'w') as f:
      f.write(f"Terraform Plan (filtered - no tags or alerts)")
      
      # Add information about ignored changes
      if coralogix_alerts or tag_block_changes > 0:
        f.write(f"\n{GRAY}Ignored changes:{RESET}")
        if coralogix_alerts:
          f.write(f"\n{GRAY}  - {len(coralogix_alerts)} unique Coralogix alerts{RESET}")
        if tag_block_changes > 0:
          f.write(f"\n{GRAY}  - {tag_block_changes} tag blocks changing ({tag_only_changes} tag-only changes){RESET}")
      
      # Count resources changes
      add_count = sum(1 for change in plan_data.get('resource_changes', []) 
                     if change.get('change', {}).get('actions', []) == ['create'])
      change_count = sum(1 for change in plan_data.get('resource_changes', []) 
                        if change.get('change', {}).get('actions', []) == ['update'])
      delete_count = sum(1 for change in plan_data.get('resource_changes', []) 
                        if change.get('change', {}).get('actions', []) == ['delete'])
      replace_count = sum(1 for change in plan_data.get('resource_changes', []) 
                         if sorted(change.get('change', {}).get('actions', [])) == sorted(['create', 'delete']))
      
      f.write(f"\n\nPlan: {GREEN}{add_count} to add{RESET}, {YELLOW}{change_count} to change{RESET}, {RED}{delete_count} to destroy{RESET}, {CYAN}{replace_count} to replace{RESET}")
      
      # Only show resources with actual changes
      has_changes = False
      
      # Write resource changes in terraform format
      for change in plan_data.get('resource_changes', []):
        actions = change.get('change', {}).get('actions', [])
        
        # Skip resources with no actions (untouched)
        if not actions:
          continue
          
        has_changes = True
        address = change.get('address', 'unknown')
        
        # Use actual terraform notation for changes with colors
        if actions == ['create']:
          prefix = f"{GREEN}+ "
          f.write(f"\n\n{prefix}{address}{RESET}")
        elif actions == ['delete']:
          prefix = f"{RED}- "
          f.write(f"\n\n{prefix}{address}{RESET}")
        elif actions == ['update']:
          prefix = f"{YELLOW}~ "
          f.write(f"\n\n{prefix}{address}{RESET}")
        elif sorted(actions) == sorted(['create', 'delete']):
          prefix = f"{CYAN}-/+ "  # replacement
          f.write(f"\n\n{prefix}{address}{RESET}")
        
        # If it's an update, show what's changing (excluding tags)
        if actions == ['update'] and 'before' in change.get('change', {}) and 'after' in change.get('change', {}):
          before = change['change']['before']
          after = change['change']['after']
          
          # If we have simplified changes, use those instead of the full diff
          if 'simplified_changes' in change.get('change', {}):
            simplified = change['change']['simplified_changes']
            for key, value in simplified.items():
              if isinstance(value, dict) and 'before' in value and 'after' in value:
                # For simple values that changed
                f.write(f"\n    {YELLOW}~ {key} = {value['before']} -> {value['after']}{RESET}")
              else:
                # For complex values, just indicate they changed
                # Try to extract relevant parts for common patterns
                if key == 'site_config' and isinstance(before.get('site_config'), list) and isinstance(after.get('site_config'), list):
                  # For site_config, try to show only what changed
                  before_config = before.get('site_config', [{}])[0] if before.get('site_config') else {}
                  after_config = after.get('site_config', [{}])[0] if after.get('site_config') else {}
                  
                  for config_key in set(list(before_config.keys()) + list(after_config.keys())):
                    if config_key in before_config and config_key in after_config and before_config[config_key] != after_config[config_key]:
                      f.write(f"\n    {YELLOW}~ {key}.{config_key} = {before_config[config_key]} -> {after_config[config_key]}{RESET}")
                else:
                  f.write(f"\n    {YELLOW}~ {key} = [Complex value changed]{RESET}")
          else:
            # Fall back to showing all differences
            for key, after_value in after.items():
              if key != 'tags' and (key not in before or before[key] != after_value):
                before_value = before.get(key, 'null')
                f.write(f"\n    {YELLOW}~ {key} = {before_value} -> {after_value}{RESET}")
        
        # If it's a replacement, show that resources will be destroyed and recreated
        if sorted(actions) == sorted(['create', 'delete']):
          f.write(f"\n    {CYAN}# This resource will be destroyed and then recreated{RESET}")
        
      # If no changes after filtering, indicate that
      if not has_changes:
        f.write(f"\n\n{GRAY}No changes to show after filtering.{RESET}")
            
  except Exception as e:
    print(f"Error filtering plan JSON: {e}")
    return None
    
  return clean_text_file

#def infracost(file_name):
#  run_cmd(f"echo -e '<details><summary>Cost Change details</summary>\n\n````````' >> {file_name}.md")
#  output = run_cmd(f"infracost diff --path {file_name}.json >> {file_name}.md")
#  for line in output:
#    run_cmd(f"{line.replace('─','_').replace('∙','-')} >> {file_name}.md")
#  run_cmd(f"infracost diff --path {file_name}.json >> {file_name}.md")
#  run_cmd(f"echo -e '````````\n</details>' >> {file_name}.md")

def tfplan(chdir, env):
  file_name = chdir.replace("/","__") + "__" + env
  workspace_exists = run_cmd(f"terraform -chdir=./{chdir} workspace list | grep '\s{env}$'")
  if not workspace_exists:
    run_cmd(f"terraform -chdir=./{chdir} workspace new {env}")
  else:
    run_cmd(f"terraform -chdir=./{chdir} workspace select {env}")
  run_cmd(f"terraform -chdir=./{chdir} plan -var-file={env}.tfvars -out {env}.tfplan -lock=false > {file_name}.txt")
  run_cmd(f"terraform -chdir=./{chdir} show -json {env}.tfplan > {file_name}.json")
  run_cmd(f"cp -r {chdir}/* {artifact_folder}/{chdir}")

  if chdir not in dirs_for_apply:
    dirs_for_apply.append(chdir)
    run_cmd(f"echo {chdir} >> {artifact_folder}/directories.txt")

  try:
    tfj2md(file_name)
  except Exception as e:
    print(e)
    print("Error with terraform-j2md tool")
  
  try:
    filter_plan_json(file_name)
  except Exception as e:
    print(e)
    print("Error filtering plan JSON")
  
  #try:
  #  infracost(file_name)
  #except: 
  #  print("Error with infracost tool")

################ MAIN ################

print(tf_path)
if tf_path == "tf_path":
  print("tf_path")
  app_paths = get_paths_for_tfplan()
else:
  app_paths = [tf_path.split("/")]

# For every path that have terraform files changed
for app_path in app_paths:
    chdir = "/".join(app_path)
    print(f"Changed dir will be: {chdir}. The current path is: {os.getcwd()}")

    # Gets all workspaces by the tfvars file
    workspaces = [workspace.replace(".tfvars", "").split("/")[-1] for workspace in glob(f"./{chdir}/*.tfvars")]
    print(f"workspaces: {workspaces}")
    run_cmd(f"mkdir -p {artifact_folder}/{chdir}")
    run_cmd(f"terraform -chdir=./{chdir} init")

    # For every workspace in the app path
    for workspace in workspaces:
    #if "main" in branch and workspace in PROD_ENV:
        if workspace in (PROD_ENV + UAT_ENV):
            tfplan(chdir, workspace)
        #elif "uat" in branch and workspace in UAT_ENV:
        #  plan(chdir, workspace)
if (fail_build == True):
  sys.exit(1)
""" else:
  print("Invalid branch, no plan required") """
