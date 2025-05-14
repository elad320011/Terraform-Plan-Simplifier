import base64
import requests
import json
import os
import argparse
import re

def strip_ansi_codes(text):
    """Remove ANSI color/style codes from text"""
    # This pattern will match all ANSI escape sequences, including the ones with [32m format
    ansi_pattern = re.compile(r'(\x1B|\033)?\[[0-9;]*[mGKHfJ]')
    return ansi_pattern.sub('', text)

def generate_auth_header(token):
    return {
        'Authorization': f'Basic {base64.b64encode(token.encode()).decode()}',
        'Content-Type': 'application/json'
    }

def determine_risk_level(file_content):
    """Determine risk level based on the number and type of changes"""
    # Count resources being added, changed, destroyed
    to_add = len(re.findall(r'will be created', file_content))
    to_change = len(re.findall(r'will be updated', file_content))
    to_destroy = len(re.findall(r'will be destroyed', file_content))
    to_replace = len(re.findall(r'must be replaced', file_content))
    
    # Calculate risk level
    if to_destroy > 5 or to_replace > 5:
        return "HIGH", "🔴", 4, to_add, to_change, to_destroy, to_replace
    elif to_destroy > 0 or to_replace > 0 or to_change > 10:
        return "MEDIUM", "🟠", 3, to_add, to_change, to_destroy, to_replace
    elif to_change > 0 or to_add > 10:
        return "LOW", "🟡", 2, to_add, to_change, to_destroy, to_replace
    elif to_add > 0:
        return "MINIMAL", "🟢", 1, to_add, to_change, to_destroy, to_replace
    else:
        return "NONE", "✅", 0, to_add, to_change, to_destroy, to_replace

def get_risk_badge(risk_level):
    """Return markdown for a risk badge based on risk level"""
    emoji_map = {
        "HIGH": "🔴",
        "MEDIUM": "🟠",
        "LOW": "🟡",
        "MINIMAL": "🟢",
        "NONE": "✅"
    }
    
    return f'{emoji_map[risk_level]} **{risk_level}**'

def generate_content(uri):
    """Generate PR comment content with enhanced formatting"""
    overall_risk_level = "NONE"
    overall_risk_emoji = "✅"
    overall_risk_value = 0
    
    # Collect all clean.txt files (filtered terraform plan outputs)
    clean_files = [f for f in os.listdir('.') if f.endswith('_clean.txt')]
    
    summary_table = []
    details = []
    file_summaries = []
    
    if not clean_files:
        header = (
            "# 🔍 Terraform Plan Summary\n"
            "This analysis was automatically generated to help understand the planned infrastructure changes.\n"
            f"[View full logs]({uri})\n"
        )
        return header + "\n\n**No Terraform changes detected in this PR.**"
    
    # Process each file
    for item in clean_files:
        with open(item, 'r') as file:
            file_content = file.read()
            
        # Extract base file name (remove _clean.txt suffix)
        base_name = item.replace('_clean.txt', '')
        
        # Extract only the relevant parts of the plan (remove ignored changes section)
        clean_content = ""
        skip_ignored = True
        for line in file_content.split('\n'):
            if line.startswith('Plan:'):
                skip_ignored = False
            if not skip_ignored:
                clean_content += line + '\n'
        
        # Clean up ANSI color codes
        file_content = strip_ansi_codes(clean_content.strip())
        
        # Parse for actual resource changes
        changes = []
        
        # Look for resources being changed in the filtered output
        resource_pattern = r'([~\-+/]+)\s+(.+)'
        resource_matches = re.findall(resource_pattern, file_content)
        
        for prefix, resource_name in resource_matches:
            # Only count actual resources, not attributes
            if not resource_name.strip().startswith('~') and '=' not in resource_name:
                changes.append(f"{prefix} {resource_name}")
        
        # Count actual changes by type
        add_count = sum(1 for c in changes if c.strip().startswith('+'))
        change_count = sum(1 for c in changes if c.strip().startswith('~'))
        destroy_count = sum(1 for c in changes if c.strip().startswith('-') and not c.strip().startswith('-/+'))
        replace_count = sum(1 for c in changes if c.strip().startswith('-/+'))
        
        # Determine risk level based on resource changes and actual detected changes
        if add_count == 0 and change_count == 0 and destroy_count == 0 and replace_count == 0:
            risk_level, risk_emoji, risk_value = "NONE", "✅", 0
        elif destroy_count > 5 or replace_count > 5:
            risk_level, risk_emoji, risk_value = "HIGH", "🔴", 4
        elif destroy_count > 0 or replace_count > 0 or change_count > 10:
            risk_level, risk_emoji, risk_value = "MEDIUM", "🟠", 3
        elif change_count > 0 or add_count > 10:
            risk_level, risk_emoji, risk_value = "LOW", "🟡", 2
        elif add_count > 0:
            risk_level, risk_emoji, risk_value = "MINIMAL", "🟢", 1
        else:
            risk_level, risk_emoji, risk_value = "NONE", "✅", 0
        
        # Track highest risk
        if risk_value > overall_risk_value:
            overall_risk_value = risk_value
            overall_risk_level = risk_level
            overall_risk_emoji = risk_emoji
        
        # Create resource summary string
        resource_parts = []
        if add_count > 0:
            resource_parts.append(f"{add_count} add")
        if change_count > 0:
            resource_parts.append(f"{change_count} change")
        if destroy_count > 0:
            resource_parts.append(f"{destroy_count} destroy")
        if replace_count > 0:
            resource_parts.append(f"{replace_count} replace")
            
        if resource_parts:
            resource_summary = ", ".join(resource_parts)
        else:
            resource_summary = "No changes"
        
        # Format the file name for display (replace __ with / and remove .tfvars)
        file_name = base_name.replace('__', '/') + '.tfvars'
        
        # Add to file summaries for summary table
        file_summaries.append({
            "file_name": file_name,
            "risk_level": risk_level,
            "risk_emoji": risk_emoji,
            "resource_summary": resource_summary,
            "item": item,
            "content": file_content,
            "changes": changes
        })
        
        # Add detailed section
        details.append(f"\n<a id='file-{item.replace('.', '').lower()}'></a>\n")
        details.append(f"## {risk_emoji} {file_name}")
        
        # Show the list of changes if there are any
        if changes:
            details.append("\n**Changes:**")
            for change in changes:
                # Clean up ANSI codes and simplify the formatting
                clean_change = strip_ansi_codes(change).strip()
                
                # Format with appropriate emoji based on the change type
                if clean_change.startswith('+'):
                    details.append(f"- ➕ {clean_change[1:].strip()}")
                elif clean_change.startswith('~'):
                    details.append(f"- 🔄 {clean_change[1:].strip()}")
                elif clean_change.startswith('-/+'):
                    details.append(f"- ♻️ {clean_change[3:].strip()}")
                elif clean_change.startswith('-'):
                    details.append(f"- ❌ {clean_change[1:].strip()}")
                else:
                    details.append(f"- {clean_change}")
            details.append("")
            
        # Add a clean plan summary before the details
        plan_summary = f"Plan: {add_count} to add, {change_count} to change, {destroy_count} to destroy, {replace_count} to replace"
        details.append(f"**{plan_summary}**\n")
            
        details.append("<details>")
        details.append("<summary>Click to expand filtered plan details</summary>\n")
        
        # Create a cleaner version of the file content with no ANSI codes
        clean_file_content = strip_ansi_codes(file_content)
        
        # Simply use code block for clean output
        details.append("```\n" + clean_file_content + "\n```")
        details.append("</details>\n")
    
    # Create header with overall risk level
    header = (
        f"# 🔍 Terraform Plan Summary — {overall_risk_emoji} {overall_risk_level} RISK\n"
        "This analysis was automatically generated to help understand the planned infrastructure changes.\n"
        f"[View full logs]({uri})\n"
        "\n## 📚 Please read How to review Terraform Pull Requests before merging.\n"
    )
    
    # Add summary table header
    summary_table.append("## Summary of Changes\n")
    summary_table.append("| File | Risk | Changes | Details |")
    summary_table.append("|------|------|---------|---------|")
    
    # Add rows to summary table
    for summary in file_summaries:
        summary_table.append(
            f"| {summary['file_name']} | "
            f"{summary['risk_emoji']} {summary['risk_level']} | "
            f"{summary['resource_summary']} | "
            f"[View Details](#file-{summary['item'].replace('.', '').lower()}) |"
        )
    
    # Combine all parts
    content = header + "\n" + "\n".join(summary_table) + "\n\n" + "\n".join(details) + "\n\n---\n"
    content += "*ℹ️ Resolve this comment before merging if changes require review*"
    
    return content

def get_thread_id(organization_uri, project, token, repository_id, pr_id):
    url = f"{organization_uri}{project}/_apis/git/repositories/{repository_id}/pullRequests/{pr_id}/threads?api-version=7.1-preview.1"
    response = requests.get(url, headers=generate_auth_header(token))
    response.raise_for_status()
    return next((thread['id'] for thread in response.json()['value']
                 if thread['comments'][0]['content'].startswith("# 🔍 Terraform Plan Summary") or
                    thread['comments'][0]['content'].startswith("# RESOLVE BEFORE MERGE")), None)

def post_comment(url, headers, content):
    response = requests.post(url, headers=headers, data=json.dumps({
        "comments": [{"parentCommentId": 0, "content": content, "commentType": "text"}],
        "status": "active"
    }))
    response.raise_for_status()
    return response.json()

def add_or_update_pr_comment(organization_uri, project, token, repository_id, pr_id, build_id):
    headers = generate_auth_header(token)
    build_uri = f"{organization_uri}/{project}/_build/results?buildId={build_id}&view=logs"
    content = generate_content(build_uri)

    thread_id = get_thread_id(organization_uri, project, token, repository_id, pr_id)
    if thread_id:
        url = f"{organization_uri}{project}/_apis/git/repositories/{repository_id}/pullRequests/{pr_id}/threads/{thread_id}/comments/1?api-version=7.1-preview.1"
        requests.patch(url, headers=headers, data=json.dumps({"content": content, "commentType": "text"})).raise_for_status()
    else:
        url = f"{organization_uri}{project}/_apis/git/repositories/{repository_id}/pullRequests/{pr_id}/threads?api-version=7.1-preview.1"
        post_comment(url, headers, content)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Add or update a PR comment in Azure DevOps.')
    parser.add_argument('--organization_uri', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--person_access_token', required=True)
    parser.add_argument('--repository_id', required=True)
    parser.add_argument('--pull_request_id', required=True)
    parser.add_argument('--build_id', required=True)

    args = parser.parse_args()
    add_or_update_pr_comment(
        args.organization_uri, args.project, args.person_access_token,
        args.repository_id, args.pull_request_id, args.build_id
    )
