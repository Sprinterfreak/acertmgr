#!/usr/bin/env python
# -*- coding: utf-8 -*-

# acertmgr - ssl management functions
# Copyright (c) Markus Hauschild & David Klaftenegger, 2016.
# available under the ISC license, see LICENSE

from OpenSSL import crypto
import base64
import binascii
import copy
import datetime
import hashlib
import json
import subprocess
import textwrap
import time
import os
import re
try:
	from urllib.request import urlopen # Python 3
except ImportError:
	from urllib2 import urlopen # Python 2

# @brief retrieve notBefore and notAfter dates of a certificate file
# @param cert_file the path to the certificate
# @return the tuple of dates: (notBefore, notAfter)
def cert_valid_times(cert_file):
	with open(cert_file) as f:
		cert_data = f.read()
	cert = crypto.load_certificate(crypto.FILETYPE_PEM, cert_data)
	asn1time = str('%Y%m%d%H%M%SZ'.encode('utf8'))
	not_before = datetime.datetime.strptime(str(cert.get_notBefore()), asn1time)
	not_after = datetime.datetime.strptime(str(cert.get_notAfter()), asn1time)
	return (not_before, not_after)

# @brief create a certificate signing request
# @param names list of domain names the certificate should be valid for
# @param key the key to use with the certificate in pyopenssl format
# @return the CSR in pyopenssl format
def cert_request(names, key):
	req = crypto.X509Req()
	req.get_subject().commonName = names[0]
	entries = ['DNS:'+name for name in names]
	extensions = [crypto.X509Extension('subjectAltName'.encode('utf8'), False, ', '.join(entries).encode('utf8'))]
	req.add_extensions(extensions)
	req.set_pubkey(key)
	req.sign(key, 'sha256')
	#return crypto.dump_certificate_request(crypto.FILETYPE_PEM, req)
	return req

# @brief convert certificate to PEM format
# @param cert certificate object in pyopenssl format
# @return the certificate in PEM format
def cert_to_pem(cert):
	return crypto.dump_certificate(crypto.FILETYPE_PEM, cert).decode('utf8')

# @brief read a key from file
# @param path path to key file
# @return the key in pyopenssl format
def read_key(path):
	with open(path) as f:
		key_data = f.read()
	return crypto.load_privatekey(crypto.FILETYPE_PEM, key_data)

# @brief create the header information for ACME communication
# @param key the account key
# @return the header for ACME
def acme_header(key):
	txt = crypto.dump_privatekey(crypto.FILETYPE_TEXT, key)
	pub_mod, pub_exp = re.search(
		r"modulus:\n\s+00:([0-9a-f:\s]+)\npublicExponent: [0-9]+ \(0x([0-9A-F]+)\)",
		txt.decode('utf8'), re.DOTALL).groups()
	pub_mod = re.sub('[:\s]', '', pub_mod)
	pub_exp = "0{0}".format(pub_exp) if len(pub_exp) % 2 else pub_exp
	header = {
		"alg": "RS256",
		"jwk": {
			"e": base64_enc(binascii.unhexlify(pub_exp.encode("utf-8"))),
			"kty": "RSA",
			"n": base64_enc(binascii.unhexlify(pub_mod.encode("utf-8"))),
		},
	}
	return header

# @brief register an account over ACME
# @param account_key the account key to register
# @param CA the certificate authority to register with
# @return True if new account was registered, False otherwise
def register_account(account_key, CA):
	header = acme_header(account_key)
	code, result = send_signed(account_key, CA, CA + "/acme/new-reg", header, {
		"resource": "new-reg",
		"agreement": "https://letsencrypt.org/documents/LE-SA-v1.0.1-July-27-2015.pdf",
	})
	if code == 201:
		print("Registered!")
		return True
	elif code == 409:
		print("Already registered!")
		return False
	else:
		raise ValueError("Error registering: {0} {1}".format(code, result))

# @brief helper function to base64 encode for JSON objects
# @param b the string to encode
# @return the encoded string
def base64_enc(b):
	return base64.urlsafe_b64encode(b).decode('utf8').replace("=", "")


# @brief helper function to make signed requests
# @param CA the certificate authority
# @param url the request URL
# @param header the message header
# @param payload the message
# @return tuple of return code and request answer
def send_signed(account_key, CA, url, header, payload):
	payload64 = base64_enc(json.dumps(payload).encode('utf8'))
	protected = copy.deepcopy(header)
	protected["nonce"] = urlopen(CA + "/directory").headers['Replay-Nonce']
	protected64 = base64_enc(json.dumps(protected).encode('utf8'))
	out = crypto.sign(account_key, '.'.join([protected64, payload64]), 'sha256')
	data = json.dumps({
		"header": header, "protected": protected64,
		"payload": payload64, "signature": base64_enc(out),
	})
	try:
		resp = urlopen(url, data.encode('utf8'))
		return resp.getcode(), resp.read()
	except IOError as e:
		return getattr(e, "code", None), getattr(e, "read", e.__str__)()

# @brief function to fetch certificate using ACME
# @param account_key the account key in pyopenssl format
# @param csr the certificate signing request in pyopenssl format
# @param domains list of domains in the certificate, first is CN
# @param acme_dir directory for ACME challanges
# @param CA which signing CA to use
# @return the certificate in pyopenssl format
# @note algorithm and parts of the code are from acme-tiny
def get_crt_from_csr(account_key, csr, domains, acme_dir, CA):
	header = acme_header(account_key)
	accountkey_json = json.dumps(header['jwk'], sort_keys=True, separators=(',', ':'))
	account_thumbprint = base64_enc(hashlib.sha256(accountkey_json.encode('utf8')).digest())

	# verify each domain
	for domain in domains:
		print("Verifying {0}...".format(domain))

		# get new challenge
		code, result = send_signed(account_key, CA, CA + "/acme/new-authz", header, {
			"resource": "new-authz",
			"identifier": {"type": "dns", "value": domain},
		})
		if code != 201:
			raise ValueError("Error requesting challenges: {0} {1}".format(code, result))

		# make the challenge file
		challenge = [c for c in json.loads(result.decode('utf8'))['challenges'] if c['type'] == "http-01"][0]
		token = re.sub(r"[^A-Za-z0-9_\-]", "_", challenge['token'])
		keyauthorization = "{0}.{1}".format(token, account_thumbprint)
		wellknown_path = os.path.join(acme_dir, token)
		with open(wellknown_path, "w") as wellknown_file:
			wellknown_file.write(keyauthorization)

		# check that the file is in place
		wellknown_url = "http://{0}/.well-known/acme-challenge/{1}".format(domain, token)
		try:
			resp = urlopen(wellknown_url)
			resp_data = resp.read().decode('utf8').strip()
			assert resp_data == keyauthorization
		except (IOError, AssertionError):
			os.remove(wellknown_path)
			raise ValueError("Wrote file to {0}, but couldn't download {1}".format(
				wellknown_path, wellknown_url))

		# notify challenge are met
		code, result = send_signed(account_key, CA, challenge['uri'], header, {
			"resource": "challenge",
			"keyAuthorization": keyauthorization,
		})
		if code != 202:
			raise ValueError("Error triggering challenge: {0} {1}".format(code, result))

		# wait for challenge to be verified
		while True:
			try:
				resp = urlopen(challenge['uri'])
				challenge_status = json.loads(resp.read().decode('utf8'))
			except IOError as e:
				raise ValueError("Error checking challenge: {0} {1}".format(
					e.code, json.loads(e.read().decode('utf8'))))
			if challenge_status['status'] == "pending":
				time.sleep(2)
			elif challenge_status['status'] == "valid":
				print("{0} verified!".format(domain))
				os.remove(wellknown_path)
				break
			else:
				raise ValueError("{0} challenge did not pass: {1}".format(
					domain, challenge_status))

	# get the new certificate
	print("Signing certificate...")
	csr_der = crypto.dump_certificate_request(crypto.FILETYPE_ASN1, csr)
	code, result = send_signed(account_key, CA, CA + "/acme/new-cert", header, {
		"resource": "new-cert",
		"csr": base64_enc(csr_der),
	})
	if code != 201:
		raise ValueError("Error signing certificate: {0} {1}".format(code, result))

	# return signed certificate!
	print("Certificate signed!")
	cert = crypto.load_certificate(crypto.FILETYPE_ASN1, result)
	return cert

